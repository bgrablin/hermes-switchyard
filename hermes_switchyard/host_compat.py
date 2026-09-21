"""Host PluginContext compatibility helpers.

Hermes 0.19.0 ships PluginContext without ``get_config``. Newer hosts expose
it as the documented config reader for ``plugin.yaml`` ``config_schema`` keys.
``register()`` must not assume the method exists, or tools/hooks/CLI never
register even when the plugin is enabled.

On hosts without ``get_config``, values fall back to
``plugins.entries.<plugin_id>.<key>`` in Hermes config.yaml (same place the
0.19 loader already stores ``allow_tool_override``). Missing entries return
the caller default so install defaults keep working.
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
    # Historical / path-derived aliases seen on this host.
    candidates.extend(["hermes-switchyard", "hermes_switchyard"])
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        raw = entries.get(name)
        if isinstance(raw, dict):
            return dict(raw)
    return {}


def ctx_get_config(ctx: Any, key: str, default: Any = None) -> Any:
    """Read a plugin config key across Hermes PluginContext generations.

    Prefer ``ctx.get_config`` when present. Otherwise read
    ``plugins.entries.<id>`` and return ``default`` when unset.
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
    """Register an auxiliary task on both old and 0.19 method names.

    Returns True when a host method accepted the registration.
    """
    # Hermes 0.19+: register_auxiliary_task(key, *, display_name, description, defaults)
    modern = getattr(ctx, "register_auxiliary_task", None)
    if callable(modern):
        modern(key, **kwargs)
        return True
    # Older / test doubles: register_auxiliary_task(key, display_name=..., ...)
    legacy = getattr(ctx, "register_auxiliary_task", None)
    if callable(legacy):
        legacy(key, **kwargs)
        return True
    return False
