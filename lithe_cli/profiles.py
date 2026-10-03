"""Multi-provider endpoint profiles: several saved endpoints, one active.

The config file (``$LITHE_HOME/config.json``, owner-only permissions — it
holds keys) grows from the original flat triple to a profile map::

    {
      "version": 2,
      "active": "zhipu",
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
    cm = data.get("cached_models")
    if isinstance(cm, list) and cm:
        out["cached_models"] = [str(m) for m in cm if str(m).strip()]
    return out


class ProfileStore:
    """Read/modify the profile file; every mutation rewrites it atomically
    enough for a CLI (write-in-place with owner-only perms, like the wizard
    always did)."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_file()

    # -- load / save ----------------------------------------------------------

    def load(self) -> dict:
        """Normalized ``{"active": str | None, "profiles": {name: endpoint}}``.

        Corrupt files and foreign shapes read as empty — a bad edit costs one
        re-prompt, not a broken CLI (same policy as the old flat loader).
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"active": None, "profiles": {}}
        if not isinstance(data, dict):
            return {"active": None, "profiles": {}}
        if "profiles" not in data:  # legacy flat triple → profile "default"
            endpoint = _clean_endpoint(data)
            if not endpoint:
                return {"active": None, "profiles": {}}
            return {"active": DEFAULT_PROFILE,
                    "profiles": {DEFAULT_PROFILE: endpoint}}
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
        return {"active": active, "profiles": profiles}

    def _save(self, active: str | None, profiles: dict) -> Path:
        fresh_dir = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fresh_dir:
            _restrict_to_owner(self.path.parent, directory=True)
        self.path.touch(exist_ok=True)
        _restrict_to_owner(self.path)
        payload = {"version": 2, "active": active, "profiles": profiles}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
        endpoint = {"api_key": api_key.strip(), "base_url": base_url.strip(),
                    "model": model.strip()}
        if context_window:
            endpoint["context_window"] = int(context_window)
        if provider and provider.strip():
            endpoint["provider"] = provider.strip()
        if document_format and document_format.strip():
            endpoint["document_format"] = document_format.strip()
        # keep a previously cached model list when the base_url is unchanged
        old = data["profiles"].get(name) or {}
        if old.get("cached_models") and old.get("base_url") == endpoint["base_url"]:
            endpoint["cached_models"] = old["cached_models"]
        data["profiles"][name] = {k: v for k, v in endpoint.items() if v}
        if data["active"] is None:
            data["active"] = name
        return self._save(data["active"], data["profiles"])

    def set_active(self, name: str) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        self._save(name, data["profiles"])
        return True

    def set_model(self, name: str, model: str) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        data["profiles"][name]["model"] = model
        self._save(data["active"], data["profiles"])
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
        self._save(data["active"], data["profiles"])
        return True

    def cache_models(self, name: str, models: list[str]) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        data["profiles"][name]["cached_models"] = list(models)
        self._save(data["active"], data["profiles"])
        return True

    def delete(self, name: str) -> bool:
        data = self.load()
        if name not in data["profiles"]:
            return False
        del data["profiles"][name]
        if data["active"] == name:
            data["active"] = next(iter(data["profiles"]), None)
        self._save(data["active"], data["profiles"])
        return True


def fetch_models(base_url: str, api_key: str, timeout: float = 10.0) -> list[str] | None:
    """GET ``{base_url}/models`` and return the model ids, or None on failure.

    Never raises: a listing is a convenience, and endpoints legitimately lack
    ``/models`` or gate it behind different auth.
    """
    url = base_url.rstrip("/") + "/models"
    req = Request(url, headers={"Authorization": f"Bearer {api_key}"})
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
