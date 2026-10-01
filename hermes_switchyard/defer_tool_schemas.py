"""Opt-in deferral of hermes_switchyard decision-tool schemas on the main model call.

When ``defer_switchyard_tool_schemas`` is on, llm_request middleware may omit the
plugin toolset schemas (``jev_assess``, ``jev_skill_select*``, ``jev_model_route*``,
``jev_session_search_rerank``) from the provider tools list on turns that will not
call those tools. Automatic skill routing via ``pre_llm_call`` is unchanged.

Capability-first: default off; when on, fail-open toward keeping schemas whenever
the turn might need Switchyard decision tools.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

# Plugin toolset only — jev_computer_use rides Hermes' computer_use toolset and
# is never deferred by this module.
DEFERRED_SWITCHYARD_TOOL_NAMES = frozenset(
    {
        "jev_assess",
        "jev_skill_select",
        "jev_skill_select_many",
        "jev_model_route",
        "jev_model_route_approved",
        "jev_session_search_rerank",
    }
)

# Explicit ask / product cues: keep schemas when the user (or prior tool use)
# indicates Switchyard decision tools are in play.
_TOOL_NAME_CUE_RE = re.compile(
    r"\b(?:"
    r"jev_assess|jev_skill_select_many|jev_skill_select|"
    r"jev_model_route_approved|jev_model_route|"
    r"jev_session_search_rerank"
    r")\b",
    re.IGNORECASE,
)
_SWITCHYARD_TOOL_ASK_RE = re.compile(
    r"\b(?:"
    r"call\s+jev_|use\s+jev_|"
    r"switchyard\s+(?:decision\s+)?tools?|"
    r"hermes_switchyard\s+tools?|"
    r"jev\s+(?:assess|skill\s*select|model\s*route|session\s*search)"
    r")\b",
    re.IGNORECASE,
)


def tool_definition_name(definition: Any) -> str | None:
    """Return the tool name from a Chat Completions or Responses-shaped tool def."""
    if not isinstance(definition, Mapping):
        return None
    function = definition.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    name = definition.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def iter_request_items(request: Mapping[str, Any] | None) -> list[Any]:
    """Return conversation items from ``messages`` or Responses ``input``.

    Responses requests may legally carry a scalar ``input`` string; normalize that
    into a single user message item so cue detection still sees the prompt.
    """
    if not isinstance(request, Mapping):
        return []
    messages = request.get("messages")
    if isinstance(messages, list):
        return messages
    items = request.get("input")
    if isinstance(items, list):
        return items
    if isinstance(items, str) and items.strip():
        return [{"role": "user", "content": items}]
    return []


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        # Responses API input_text / output_text
        if part.get("type") in {"text", "input_text", "output_text"} and isinstance(
            part.get("text"), str
        ):
            parts.append(part["text"])
    return "\n".join(parts)


def latest_user_text(request: Mapping[str, Any] | None) -> str:
    """Best-effort latest user message text from provider kwargs."""
    for item in reversed(iter_request_items(request)):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().lower()
        # Responses API may use type=message with role
        item_type = str(item.get("type") or "").strip().lower()
        if role == "user" or (item_type == "message" and role == "user"):
            text = _text_from_content(item.get("content")).strip()
            if text:
                return text
    return ""


def _tool_call_names_from_item(item: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    # Chat Completions assistant tool_calls
    tool_calls = item.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if isinstance(function, Mapping):
                name = function.get("name")
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
            name = call.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    # Anthropic assistant tool_use content blocks.
    content = item.get("content")
    if item.get("role") == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
    # Responses function_call items
    if str(item.get("type") or "").strip().lower() == "function_call":
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def history_has_switchyard_tool_call(request: Mapping[str, Any] | None) -> bool:
    """True when prior turns already invoked a deferred Switchyard decision tool."""
    for item in iter_request_items(request):
        if not isinstance(item, Mapping):
            continue
        if _tool_call_names_from_item(item) & DEFERRED_SWITCHYARD_TOOL_NAMES:
            return True
    return False


def user_requests_switchyard_tools(text: str) -> bool:
    """True when the user message explicitly asks for Switchyard decision tools."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(_TOOL_NAME_CUE_RE.search(stripped) or _SWITCHYARD_TOOL_ASK_RE.search(stripped))


def tools_are_switchyard_primary(tools: Sequence[Any] | None) -> bool:
    """True when every named tool is a deferred Switchyard tool (explicit pin).

    Fail-open: if the session offered only hermes_switchyard decision tools,
    do not strip them — the toolset is the product for that session.
    """
    if not tools:
        return False
    named = [tool_definition_name(t) for t in tools]
    concrete = [n for n in named if n]
    if not concrete:
        return False
    return all(n in DEFERRED_SWITCHYARD_TOOL_NAMES for n in concrete)


def should_omit_switchyard_tool_schemas(
    *,
    enabled: bool,
    request: Mapping[str, Any] | None,
    user_text: str | None = None,
) -> bool:
    """Return whether to omit hermes_switchyard schemas from this provider request.

    Fail-open toward capability: keep schemas when the flag is off, when the user
    asks for decision tools, when history already called them, when the toolset
    pin is Switchyard-primary, or when the request has no tools list to edit.
    """
    if enabled is not True:
        return False
    if not isinstance(request, Mapping):
        return False
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        # Nothing to omit, or tools live under another key we do not rewrite.
        return False
    # Explicit provider tool choices must remain satisfiable.
    choice = request.get("tool_choice")
    if isinstance(choice, Mapping) and tool_definition_name(choice) in DEFERRED_SWITCHYARD_TOOL_NAMES:
        return False
    text = latest_user_text(request) if user_text is None else (user_text or "")
    if not text:
        return False  # Unknown or non-text request shapes fail open.
    if user_requests_switchyard_tools(text):
        return False
    # Keep an explicit request through later clarifications, even before the
    # decision tool has run. A prior actual call also keeps schemas below.
    if any(isinstance(item, Mapping) and item.get("role") == "user"
           and user_requests_switchyard_tools(_text_from_content(item.get("content")))
           for item in iter_request_items(request)):
        return False
    if history_has_switchyard_tool_call(request):
        return False
    if tools_are_switchyard_primary(tools):
        return False
    # Skill-route-only / light oneshots and other turns that do not cue decision
    # tools: omit. Automatic skill pre_llm_call still runs independently.
    return True


def filter_switchyard_tool_schemas(tools: Sequence[Any] | None) -> list[Any]:
    """Return a tools list with deferred Switchyard decision tools removed."""
    if not tools:
        return []
    kept: list[Any] = []
    for definition in tools:
        name = tool_definition_name(definition)
        if name in DEFERRED_SWITCHYARD_TOOL_NAMES:
            continue
        kept.append(definition)
    return kept


def omit_switchyard_tools_from_request(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a shallow-copied request with Switchyard decision tools omitted."""
    out = dict(request)
    tools = request.get("tools")
    if isinstance(tools, list):
        out["tools"] = filter_switchyard_tool_schemas(tools)
    return out


def compose_llm_request_middleware(*callbacks: Any):
    """Chain ``llm_request`` callbacks so each sees the prior rewrite.

    Hermes may invoke registered callbacks with the original payload and keep
    only the last returned ``request``. Composing into one callback ensures
    adaptive-effort and schema-deferral both land on the wire.
    """
    active = [cb for cb in callbacks if callable(cb)]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def on_llm_request(
        request: Mapping[str, Any] | None = None,
        **context: Any,
    ) -> dict[str, Any] | None:
        current: Mapping[str, Any] = request if isinstance(request, Mapping) else {}
        last: dict[str, Any] | None = None
        for callback in active:
            try:
                out = callback(request=current, **context)
            except TypeError:
                # Some callbacks accept a positional request only.
                try:
                    out = callback(current, **context)
                except Exception:  # noqa: BLE001 -- never break the provider call
                    continue
            except Exception:  # noqa: BLE001 -- never break the provider call
                continue
            if not isinstance(out, Mapping):
                continue
            next_request = out.get("request")
            if isinstance(next_request, Mapping):
                current = next_request
            merged = dict(out)
            merged["request"] = dict(current)
            last = merged
        return last

    return on_llm_request


def build_defer_tool_schemas_middleware(*, enabled: bool):
    """Build an ``llm_request`` middleware callback (or None when disabled)."""
    if enabled is not True:
        return None

    def on_llm_request(
        request: Mapping[str, Any] | None = None,
        **_context: Any,
    ) -> dict[str, Any] | None:
        raw = request if isinstance(request, Mapping) else {}
        try:
            if not should_omit_switchyard_tool_schemas(enabled=True, request=raw):
                return None
            updated = omit_switchyard_tools_from_request(raw)
        except Exception:  # noqa: BLE001 -- never break the provider call
            return None
        if updated.get("tools") is raw.get("tools"):
            return None
        return {
            "request": updated,
            "source": "hermes-switchyard",
            "reason": "defer_switchyard_tool_schemas",
            "name": "defer_switchyard_tool_schemas",
        }

    return on_llm_request


def register_defer_tool_schemas_middleware(
    ctx: Any,
    *,
    enabled: bool,
    chain_with: Any = None,
    register: bool = True,
) -> dict[str, Any]:
    """Register llm_request middleware when the flag is on and the seam exists.

    When ``chain_with`` is a prior ``llm_request`` callback (for example adaptive
    effort), compose schema filtering after it and register the single composed
    callback so last-callback-wins hosts cannot drop the prior rewrite.
    """
    if enabled is not True and not callable(chain_with):
        return {"registered": False, "reason": "flag_off", "enabled": False}
    register_middleware = getattr(ctx, "register_middleware", None)
    if register and not callable(register_middleware):
        return {
            "registered": False,
            "reason": "hermes_llm_request_middleware_unavailable",
            "enabled": bool(enabled),
        }
    defer_cb = build_defer_tool_schemas_middleware(enabled=enabled is True)
    if callable(chain_with) and defer_cb is not None:
        callback = compose_llm_request_middleware(chain_with, defer_cb)
        composed = True
    elif defer_cb is not None:
        callback = defer_cb
        composed = False
    elif callable(chain_with):
        callback = chain_with
        composed = False
    else:
        return {"registered": False, "reason": "flag_off", "enabled": False}
    if register:
        register_middleware("llm_request", callback)
    return {
        "registered": bool(register),
        "reason": "ok",
        "enabled": bool(enabled),
        "composed_with_prior": composed,
    }
