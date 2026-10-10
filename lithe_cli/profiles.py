"""Multi-provider endpoint profiles: several saved endpoints, one active.

The config file (``$LITHE_HOME/config.json``, owner-only permissions — it
holds keys) grows from the original flat triple to a profile map::

    {
      "version": 2,
      "active": "zhipu",
      "favorites": ["zhipu:glm-4.6", "anthropic:claude-sonnet-4"],
      "profiles": {
        "zhipu": {"base_url": "...", "api_key": "...", "model": "glm-4.6",
                  "context_window": 128000, "cached_models": ["glm-4.6"]},
        "openrouter": {"base_url": "...", "api_key": "...", "model": "...",
                        "provider": "openrouter"},
        "selfhost": {"base_url": "https://my-router/api/v1", "api_key": "...",
                      "model": "...", "provider": "openrouter",
                      "document_format": "inline-file"}
      }
    }

``provider`` names a vendor preset (format family) and only fills values
left unset — an explicit ``base_url`` always wins, so a self-built router
speaking the OpenRouter format keeps its own URL. ``document_format``
overrides the preset's document-block dialect for ``analyze_document``
(see lithe.bundles.documents).

``favorites`` are qualified model refs (``profile:model``) shown as the
"常用" section by the model pickers — the quick-switch lane across
providers. Entries whose profile has been deleted are pruned on load.

A legacy flat ``{api_key, base_url, model}`` file is translated in memory to
the single profile ``default`` and is never rewritten until an explicit save
(a mutation through this module) — old installs keep working untouched.

Profile selection order matches the endpoint keys: the ``--profile`` flag >
``LITHE_PROFILE`` > the saved ``active`` marker. Within the chosen profile,
per-key resolution stays flag > env > profile field (see config.load_config),
so CI can still pin ``LITHE_MODEL`` over whatever profile is active.

No default endpoints exist here, ever — profiles contain exactly what the
user typed into the wizard or wrote by hand.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import _restrict_to_owner, config_file

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENDPOINT_KEYS = ("api_key", "base_url", "model")
DEFAULT_PROFILE = "default"


def valid_name(name: str) -> bool:
    return bool(PROFILE_NAME_RE.match(name or ""))


def masked_key(key: str | None) -> str:
    if not key:
        return ""
    return key if len(key) <= 8 else f"{key[:5]}…{key[-4:]}"


def _clean_endpoint(data: dict) -> dict:
    """Keep the known string/int fields; strip blanks and foreign keys."""
    out: dict[str, Any] = {}
    for k in ENDPOINT_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    provider = data.get("provider")
    if isinstance(provider, str) and provider.strip():
        out["provider"] = provider.strip()
    effort = data.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        out["reasoning_effort"] = effort.strip()
    dfmt = data.get("document_format")
    if isinstance(dfmt, str) and dfmt.strip():
        out["document_format"] = dfmt.strip()
    for k in ("context_window",):
        v = data.get(k)
        if isinstance(v, int) and v > 0:
            out[k] = v
    temperature = data.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        out["temperature"] = float(temperature)
    max_tokens = data.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) \
            and max_tokens > 0:
        out["max_tokens"] = max_tokens
    extra_body = data.get("extra_body")
    if isinstance(extra_body, dict) and extra_body:
        out["extra_body"] = extra_body
    headers = data.get("default_headers")
    if isinstance(headers, dict) and headers:
        out["default_headers"] = headers
    pricing = data.get("pricing")
    if isinstance(pricing, dict) and pricing:
        # keep numeric entries only; LLMConfig validates the exact contract
        out["pricing"] = {k: v for k, v in pricing.items()
                          if isinstance(v, (int, float))
                          and not isinstance(v, bool)}
    cm = data.get("cached_models")
    if isinstance(cm, list) and cm:
        out["cached_models"] = [str(m) for m in cm if str(m).strip()]
    return out


def _clean_favorites(raw: Any, profiles: dict) -> list[str]:
    """Validated ``profile:model`` refs pointing at live profiles only."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or ":" not in item:
            continue
        profile, _, model = item.partition(":")
        if profile in profiles and model.strip() and item not in out:
            out.append(item)
    return out


class ProfileStore:
    """Read/modify the profile file; every mutation rewrites it via an
    atomic temp-file replace (the previous version survives as ``.bak``)
    with owner-only perms — the file holds every saved API key."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_file()

    # -- load / save ----------------------------------------------------------

    def _read_json(self, path: Path):
        """Parsed JSON at *path*, or None when unreadable/corrupt."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def load(self) -> dict:
        """Normalized ``{"active", "profiles", "favorites"}``.

        A corrupt main file falls back to the ``.bak`` left by the last
        atomic save before degrading to empty — a bad edit or a torn write
        costs the edit, not every saved profile. Favorites whose profile no
        longer exists are pruned here, so every read (and the save round
        trips built on it) never sees ghost entries.
        """
        data = self._read_json(self.path)
        if data is None:
            data = self._read_json(
                self.path.parent / (self.path.name + ".bak"))
        if data is None or not isinstance(data, dict):
            return {"active": None, "profiles": {}, "favorites": []}
        if "profiles" not in data:  # legacy flat triple → profile "default"
            endpoint = _clean_endpoint(data)
            if not endpoint:
                return {"active": None, "profiles": {}, "favorites": []}
            return {"active": DEFAULT_PROFILE,
                    "profiles": {DEFAULT_PROFILE: endpoint},
                    "favorites": []}
        profiles_raw = data.get("profiles")
        profiles = {}
        if isinstance(profiles_raw, dict):
            for name, ep in profiles_raw.items():
                if isinstance(name, str) and valid_name(name) \
                        and isinstance(ep, dict):
                    endpoint = _clean_endpoint(ep)
                    if endpoint:
                        profiles[name] = endpoint
        active = data.get("active")
        if active not in profiles:
            active = next(iter(profiles), None)
        favorites = _clean_favorites(data.get("favorites"), profiles)
        return {"active": active, "profiles": profiles,
                "favorites": favorites}

    def _save(self, data: dict) -> Path:
        fresh_dir = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fresh_dir:
            _restrict_to_owner(self.path.parent, directory=True)
        payload = {"version": 2, "active": data.get("active"),
                   "favorites": data.get("favorites") or [],
                   "profiles": data["profiles"]}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        # Atomic replace: a crash mid-write must never destroy every saved
        # profile (API keys included). The old file survives as .bak for the
        # load-time fallback; a failed rename chain leaves one of the two
        # intact on disk.
        tmp = self.path.parent / (self.path.name + ".tmp")
        bak = self.path.parent / (self.path.name + ".bak")
        tmp.write_text(text, encoding="utf-8")
        _restrict_to_owner(tmp)
        if self.path.exists():
            try:
                os.replace(self.path, bak)
                _restrict_to_owner(bak)  # the backup holds keys too
            except OSError:
                pass  # best effort; the main replace below is what matters
        os.replace(tmp, self.path)
        return self.path

    # -- queries --------------------------------------------------------------

    def names(self) -> list[str]:
        return list(self.load()["profiles"])

    def active_name(self, selected: str | None = None) -> str | None:
        data = self.load()
        if selected is not None:
            return selected if selected in data["profiles"] else None
        return data["active"]

    def endpoint(self, name: str | None = None) -> dict:
        """Endpoint dict for *name* (default: the active profile); {} if none.

        Raises SystemExit for an explicitly requested unknown profile — a
        typo'd ``--profile`` must fail loudly, not silently fall back.
        """
        data = self.load()
        if name is None:
            name = data["active"]
        if name is None:
            return {}
        if name not in data["profiles"]:
            known = "、".join(sorted(data["profiles"])) or "（无）"
            raise SystemExit(
                f"未知档案 {name}。可用档案：{known}。"
                "用 lithe-cli config 交互配置，或 lithe-cli config --list 查看。"
            )
        return dict(data["profiles"][name])

    def masked(self, name: str | None = None) -> dict:
        ep = self.endpoint(name)
        ep["api_key"] = masked_key(ep.get("api_key"))
        return ep

    # -- mutations ------------------------------------------------------------

    def upsert(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        context_window: int | None = None,
        provider: str | None = None,
        document_format: str | None = None,
    ) -> Path:
        if not valid_name(name):
            raise SystemExit(
                f"档案名 {name!r} 不合法（仅限字母、数字、点、下划线、连字符）"
            )
        data = self.load()
        # Start from the existing endpoint so fields the caller doesn't
        # know about — provider, reasoning_effort, document_format, a
        # hand-tuned context_window — survive a re-config instead of being
        # silently erased. The caller's triple always overwrites.
        endpoint = dict(data["profiles"].get(name) or {})
        endpoint.update({"api_key": api_key.strip(),
                         "base_url": base_url.strip(),
                         "model": model.strip()})
        if context_window:
            endpoint["context_window"] = int(context_window)
        if provider and provider.strip():
            endpoint["provider"] = provider.strip()
        if document_format and document_format.strip():
            endpoint["document_format"] = document_format.strip()
        # a cached model list is only meaningful for the same endpoint
        if endpoint.get("cached_models") \
                and data["profiles"].get(name, {}).get("base_url") \
                != endpoint["base_url"]:
            endpoint.pop("cached_models", None)
        data["profiles"][name] = {k: v for k, v in endpoint.items() if v}
        if data["active"] is None:
            data["active"] = name
        return self._save(data)

    def set_active(self, name: str) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        data["active"] = name
        self._save(data)
        return True

    def set_model(self, name: str, model: str) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        data["profiles"][name]["model"] = model
        self._save(data)
        return True

    def set_provider(self, name: str, provider: str | None) -> bool:
        """Set (or clear, with None/""/``none``/``off``) a profile's provider.

        An unknown name raises the preset module's :class:`ValueError`
        (its message lists the available presets) — a typo'd provider
        must not silently strip the profile's protocol. Returns False
        only for an unknown *profile* name.
        """
        data = self.load()
        if name not in data["profiles"]:
            return False
        endpoint = data["profiles"][name]
        raw = (provider or "").strip()
        if not raw or raw.lower() in ("none", "off"):
            endpoint.pop("provider", None)
        else:
            try:
                from lithe.bundles.providers import get_preset
            except ImportError:  # kernel not importable in this environment
                get_preset = None
            if get_preset is not None:
                get_preset(raw)  # ValueError on unknown, listing options
            endpoint["provider"] = raw.lower()
        self._save(data)
        return True

    def set_reasoning_effort(self, name: str, effort: str | None) -> bool:
        """Set (or clear, with None/"off") a profile's reasoning effort."""
        data = self.load()
        if name not in data["profiles"]:
            return False
        endpoint = data["profiles"][name]
        if effort is None or effort.strip().lower() == "off":
            endpoint.pop("reasoning_effort", None)
        else:
            endpoint["reasoning_effort"] = effort.strip()
        self._save(data)
        return True

    def cache_models(self, name: str, models: list[str]) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        data["profiles"][name]["cached_models"] = list(models)
        self._save(data)
        return True

    def delete(self, name: str) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        del data["profiles"][name]
        if data["active"] == name:
            data["active"] = next(iter(data["profiles"]), None)
        # favorites pointing at the deleted profile die with it
        data["favorites"] = _clean_favorites(
            data.get("favorites"), data["profiles"])
        self._save(data)
        return True

    # -- favorites -------------------------------------------------------------

    def favorites(self) -> list[str]:
        """Qualified ``profile:model`` refs (pruned to live profiles)."""
        return self.load()["favorites"]

    def set_favorites(self, items: list[str]) -> bool:
        data = self.load()
        data["favorites"] = _clean_favorites(items, data["profiles"])
        self._save(data)
        return True

    def toggle_favorite(self, qualified: str) -> bool | None:
        """Add/remove a qualified ref. Returns the new state (True = now a
        favorite), or None when the ref is malformed / its profile is
        unknown — a typo'd favorite must fail loudly, not save garbage."""
        profile, _, model = (qualified or "").partition(":")
        data = self.load()
        if not model.strip() or profile not in data["profiles"]:
            return None
        ref = f"{profile}:{model.strip()}"
        favs = data["favorites"]
        if ref in favs:
            favs.remove(ref)
            added = False
        else:
            favs.append(ref)
            added = True
        self._save(data)
        return added


def _fetch_models_url(url: str, headers: dict, timeout: float) -> list[str] | None:
    """GET *url* and parse ``data[].id`` items; None on any failure."""
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (URLError, OSError, TimeoutError, ValueError):
        return None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    ids = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
        elif isinstance(item, str):
            ids.append(item)
    return ids or None


def fetch_models(base_url: str, api_key: str, timeout: float = 10.0) -> list[str] | None:
    """GET ``{base_url}/models`` (Bearer auth) and return the model ids, or
    None on failure.

    Never raises: a listing is a convenience, and endpoints legitimately lack
    ``/models`` or gate it behind different auth.
    """
    url = base_url.rstrip("/") + "/models"
    return _fetch_models_url(
        url, {"Authorization": f"Bearer {api_key}"}, timeout)


def models_request(endpoint: dict) -> tuple[str, dict[str, str]] | None:
    """Protocol-aware ``(url, headers)`` for a GET ``{base_url}/models``.

    The endpoint's provider preset names its transport: a ``messages``
    endpoint authenticates with ``x-api-key`` + ``anthropic-version``
    (and takes a ``limit`` so the default 20-model page doesn't silently
    truncate the list); everything else keeps the OpenAI-style Bearer
    header. A missing/unknown provider degrades to Bearer; None when the
    endpoint lacks ``base_url`` / ``api_key``.
    """
    base_url = (endpoint.get("base_url") or "").rstrip("/")
    api_key = endpoint.get("api_key") or ""
    if not (base_url and api_key):
        return None
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    provider = endpoint.get("provider")
    if provider:
        try:
            from lithe.bundles.providers import get_preset
        except ImportError:  # kernel not importable in this environment
            get_preset = None
        if get_preset is not None:
            try:
                preset = get_preset(provider)
            except ValueError:
                preset = None
            if preset and preset.get("transport") == "messages":
                url = f"{base_url}/models?limit=1000"
                headers = {"x-api-key": api_key,
                           "anthropic-version": "2023-06-01"}
    return url, headers


def fetch_models_for(endpoint: dict, timeout: float = 10.0) -> list[str] | None:
    """List models for a stored profile with its protocol's own auth
    (:func:`models_request`). Both flavors answer with the same
    ``{"data": [{"id": ...}]}`` shape. Never raises: a listing is a
    convenience, and endpoints legitimately lack ``/models`` or gate it
    behind different auth — None on failure.
    """
    request = models_request(endpoint)
    if request is None:
        return None
    url, headers = request
    return _fetch_models_url(url, headers, timeout)
