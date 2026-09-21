"""Host PluginContext compatibility helpers.

Hermes 0.19.0 ships PluginContext without ``get_config``. Newer hosts expose
it as the documented config reader for ``plugin.yaml`` ``config_schema`` keys.
``register()`` must not assume the method exists, or tools/hooks/CLI never
register even when the plugin is enabled.

On hosts without ``get_config``, values fall back to
``plugins.entries.<plugin_id>.settings`` in Hermes config.yaml (legacy
``plugins.entries.<plugin_id>.config`` is also accepted). Missing entries
return the caller default so install defaults keep working.
"""

from __future__ import annotations

from typing import Any


def plugin_entry_id(ctx: Any) -> str | None:
    """Best-effort plugin id for ``plugins.entries`` lookups."""
    manifest = getattr(ctx, "manifest", None)
    if manifest is None:
        return None
    for attr in ("key", "name"):
        value = getattr(manifest, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _settings_from_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract user settings from a Hermes plugin entry mapping.

    Hermes stores operator values under ``settings`` (current) or ``config``
    (legacy). The outer entry also carries host fields such as
    ``allow_tool_override``; those are not plugin config_schema keys.
    """
    settings = raw.get("settings")
    if isinstance(settings, dict):
        return dict(settings)
    legacy = raw.get("config")
    if isinstance(legacy, dict):
        return dict(legacy)
    return {}


def _entry_settings(ctx: Any) -> dict[str, Any]:
    plugin_id = plugin_entry_id(ctx)
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config() or {}
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    entries = ((cfg.get("plugins") or {}).get("entries") or {})
    if not isinstance(entries, dict):
        return {}
    candidates: list[str] = []
    if plugin_id:
        candidates.append(plugin_id)
    # Historical / path-derived aliases.
    candidates.extend(["hermes-switchyard", "hermes_switchyard"])
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        raw = entries.get(name)
        if isinstance(raw, dict):
            return _settings_from_entry(raw)
    return {}


def ctx_get_config(ctx: Any, key: str, default: Any = None) -> Any:
    """Read a plugin config key across Hermes PluginContext generations.

    Prefer ``ctx.get_config`` when present. Otherwise read
    ``plugins.entries.<id>.settings`` (or legacy ``.config``) and return
    ``default`` when unset.
    """
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        try:
            return getter(key, default=default)
        except TypeError:
            # Some stubs use positional default only.
            return getter(key, default)

    settings = _entry_settings(ctx)
    if key in settings:
        return settings[key]
    return default


def register_auxiliary_task(ctx: Any, key: str, **kwargs: Any) -> bool:
    """Register an auxiliary task when the host exposes the API.

    Returns True when a host method accepted the registration.
    """
    method = getattr(ctx, "register_auxiliary_task", None)
    if callable(method):
        method(key, **kwargs)
        return True
    return False
