"""Jev structured-decision plugin for Hermes."""

from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from . import receipt_state, schemas
from .automatic import _config_float, build_pre_llm_call_hook, discover_mandatory_skills
from .client import (
    DEFAULT_ENDPOINT,
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    MAX_DECISION_REQUESTS,
    TYPESAFE_ENDPOINT,
    DecisionClient,
    PartialAccountingError,
    request_budget_scope,
)
from . import browser_use
from .computer_use import StaleTargetError, run_computer_goal
from .egress import is_routing_mode
from .routing import route_model, select_skill, select_skills


_UNREGISTERED_RUNTIME_STATUS = {
    "plugin_loaded": False,
    "routing_mode": None,
    "consumer_mode": None,
    "public_or_sanitized_data_ack": None,
    "automatic_skill_jev_mode": None,
    "hosted_construction_allowed": False,
}
_RUNTIME_STATUS = dict(_UNREGISTERED_RUNTIME_STATUS)


def reset_runtime_status() -> None:
    """Clear register-time status. Tests use this to model a fresh process."""
    _RUNTIME_STATUS.clear()
    _RUNTIME_STATUS.update(_UNREGISTERED_RUNTIME_STATUS)


def _publish_runtime_status(
    *,
    routing_mode: Any,
    consumer_mode: Any,
    public_or_sanitized_data_ack: bool,
    automatic_skill_jev_mode: Any,
) -> None:
    ack = public_or_sanitized_data_ack is True
    mode = routing_mode if is_routing_mode(routing_mode) else None
    _RUNTIME_STATUS.update(
        {
            "plugin_loaded": True,
            "routing_mode": mode,
            "consumer_mode": consumer_mode if consumer_mode in {"advisory", "load"} else None,
            "public_or_sanitized_data_ack": ack,
            "automatic_skill_jev_mode": (
                automatic_skill_jev_mode if automatic_skill_jev_mode in {"always", "uncertain_only"} else None
            ),
            "hosted_construction_allowed": bool(mode == "hosted_sanitized" and ack),
        }
    )


def _after_install_text() -> str:
    """Return the install-time next-step text without network access."""
    path = Path(__file__).resolve().parent.parent / "after-install.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    return (
        "Run hermes switchyard setup --provider typesafe "
        "(or --provider openrouter), then start a fresh session."
    )


def _cli_handler(args):
    command = getattr(args, "switchyard_command", None) or getattr(args, "jev_command", None)
    if command == "status":
        credential_presence = {}
        for provider in ("typesafe", "openrouter"):
            try:
                credential_presence[provider] = bool(_secret(provider))
            except Exception:  # local secret-store status can be unavailable or locked
                credential_presence[provider] = False
        payload = {
            "plugin": "hermes-switchyard",
            "status": "ready" if any(credential_presence.values()) else "credential_required",
            "network": False,
            "credential_presence": credential_presence,
            "plugin_loaded": _RUNTIME_STATUS["plugin_loaded"],
            "routing_mode": _RUNTIME_STATUS["routing_mode"],
            "consumer_mode": _RUNTIME_STATUS["consumer_mode"],
            "public_or_sanitized_data_ack": _RUNTIME_STATUS["public_or_sanitized_data_ack"],
            "automatic_skill_jev_mode": _RUNTIME_STATUS["automatic_skill_jev_mode"],
            "hosted_construction_allowed": _RUNTIME_STATUS["hosted_construction_allowed"],
        }
        if getattr(args, "json_output", False):
            print(json.dumps(payload, sort_keys=True))
        elif payload["status"] == "credential_required":
            print(
                "Hermes Switchyard: credential_required (local status only). "
                "Run: hermes switchyard setup --provider typesafe"
            )
        else:
            print(f"Hermes Switchyard: {payload['status']} (local status only)")
        return 0
    if command == "guide":
        print(_after_install_text())
        return 0
    if command == "test":
        if getattr(args, "live", False) is not True or getattr(args, "public_or_sanitized_data_ack", False) is not True:
            print("Refusing live test: pass --live and --public-or-sanitized-data-ack.")
            return 2
        requested_provider = getattr(args, "provider", "auto")
        provider = requested_provider
        if provider == "auto":
            provider = "typesafe" if _secret("typesafe") else "openrouter"
        key = _secret(provider)
        if not key:
            print(json.dumps({"status": "failed", "reason": "credential_required"}, sort_keys=True))
            return 1
        endpoint = TYPESAFE_ENDPOINT if provider == "typesafe" else DEFAULT_ENDPOINT
        model = "jev-latest" if provider == "typesafe" else "typesafe/jev-1.13"
        active_client = DecisionClient(api_key=key, endpoint=endpoint, model=model)
        try:
            result = active_client.decide(
                {
                    "task": "Hermes Switchyard explicitly billed connectivity test",
                    "data_class": "public_synthetic",
                },
                {
                    "connectivity": {
                        "type": "noul",
                        "instructions": "Is this a bounded public synthetic connectivity test?",
                    }
                },
                public_or_sanitized_data_ack=True,
            )
        except Exception:  # provider and transport text must not reach CLI output
            print(json.dumps({"status": "failed", "reason": "provider_request_failed"}, sort_keys=True))
            return 1
        finally:
            active_client.close()
        print(json.dumps({
            "status": "passed",
            "provider": provider,
            "model": result.get("model"),
            "request_id": result.get("request_id"),
            "latency_ms": result.get("latency_ms"),
            "usage": result.get("usage") or {},
        }, sort_keys=True))
        return 0
    if command == "receipt":
        receipt = receipt_state.read_latest_receipt()
        if receipt is None:
            print(json.dumps({"status": "unavailable", "reason": "no_receipt"}, sort_keys=True))
            return 1
        indent = None if getattr(args, "json_output", False) else 2
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=indent))
        return 0
    if command != "setup":
        print("Usage: hermes switchyard <status|guide|setup|receipt|test> [--provider ...|--json]")
        return 2
    provider = args.provider
    key_name = "TYPESAFE_API_KEY" if provider == "typesafe" else "OPENROUTER_API_KEY"
    from hermes_cli.config import save_env_value
    from hermes_cli.secret_prompt import masked_secret_prompt

    value = masked_secret_prompt(f"{key_name}: ").strip()
    if not value:
        print("No credential saved.")
        return 1
    save_env_value(key_name, value)
    print(f"Saved {key_name} to the active Hermes profile secret store. Start a fresh session.")
    return 0


def _setup_cli(parser):
    commands = parser.add_subparsers(dest="switchyard_command")
    setup = commands.add_parser("setup", help="Save one Jev provider key through a masked prompt")
    setup.add_argument("--provider", required=True, choices=("typesafe", "openrouter"))
    receipt = commands.add_parser("receipt", help="Show the latest automatic-routing receipt")
    receipt.add_argument("--json", action="store_true", dest="json_output", help="Emit compact JSON")
    commands.add_parser("status", help="Show local readiness without network access").add_argument("--json", action="store_true", dest="json_output")
    commands.add_parser("guide", help="Show local usage guidance")
    test = commands.add_parser("test", help="Run the explicitly billed live provider test")
    test.add_argument("--live", action="store_true", help="Confirm that this may make a billed request")
    test.add_argument("--public-or-sanitized-data-ack", action="store_true", dest="public_or_sanitized_data_ack")
    test.add_argument("--provider", choices=("auto", "typesafe", "openrouter"), default="auto")
    parser.set_defaults(func=_cli_handler)


def _secret(provider: str = "auto"):
    from agent.secret_scope import get_secret

    names = {
        "typesafe": ("TYPESAFE_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY",),
        "auto": ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"),
    }
    if provider not in names:
        raise ValueError("jev_provider must be auto, typesafe, or openrouter")
    for name in names[provider]:
        value = str(get_secret(name) or "").strip()
        if value:
            return value
    return ""


def _partial_accounting(exc_partial: Any) -> dict[str, Any]:
    """Summarize successful hosted calls recorded before a later failure."""
    from .automatic import _partial_accounting_metadata

    metadata = _partial_accounting_metadata(exc_partial)
    if not metadata:
        return {}
    return metadata


def _copy_redacted_jev_metadata_from_partial(exc_partial: Any) -> dict[str, Any]:
    """Copy redacted partial-accounting metadata without provider text."""
    summary = _partial_accounting(exc_partial)
    if not summary:
        return {}
    # Only bounded local metadata survives: counts, numeric latencies, typed
    # usage numbers, and identifiers that pass the receipt-safe identifier
    # carrier never receives arbitrary provider strings.
    allowed: dict[str, Any] = {}
    for field in ("request_count", "total_latency_ms"):
        value = summary.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            allowed[field] = value
    usage = summary.get("total_usage")
    if isinstance(usage, dict):
        bounded_usage = receipt_state.safe_usage(usage)
        if bounded_usage:
            allowed["total_usage"] = bounded_usage
            cost_is_unknown = bounded_usage.get("cost", "absent") is None
            allowed["cost_known"] = not cost_is_unknown
    for field in ("model", "request_id"):
        value = summary.get(field)
        if isinstance(value, str):
            bounded = receipt_state.safe_identifier(value, max_length=128)
            if bounded is not None:
                allowed[field] = bounded
    return allowed


def _error(exc):
    # Preserve observability with stable local codes, never arbitrary provider,
    # executor, UI, or credential text.
    if isinstance(exc, PermissionError):
        code, reason = "ack_required", "public_or_sanitized_data_ack is false"
    elif isinstance(exc, StaleTargetError):
        code, reason = "stale_target", "the exposed target identity changed"
    elif isinstance(exc, ValueError):
        code, reason = "invalid_request", "request validation failed"
    elif isinstance(exc, TypeError):
        code, reason = "invalid_response", "structured response validation failed"
    elif isinstance(exc, RuntimeError):
        code, reason = "execution_failed", "the bounded operation was not accepted"
    else:
        code, reason = "plugin_error", "the plugin operation failed"
    error = {"code": code, "reason": reason}
    # Partial accounting evidence recorded before a later failure survives the
    # public boundary through the same redacted metadata contract the success
    # path uses; the known subtotal is marked incomplete.
    if isinstance(exc, PartialAccountingError):
        partial_metadata = _copy_redacted_jev_metadata_from_partial(exc.partial)
        if partial_metadata:
            error["partial_accounting"] = partial_metadata
            error["total_usage_incomplete"] = True
    return json.dumps({"status": "error", "error": error})


def _resolved_public_data_ack(args, standing=True):
    if standing is not True:
        return False
    if "public_or_sanitized_data_ack" in args:
        return args.get("public_or_sanitized_data_ack") is True
    return True


def _require_public_data_ack(args, standing=True):
    if _resolved_public_data_ack(args, standing) is not True:
        raise PermissionError(
            "public_or_sanitized_data_ack is false; this call was refused"
        )


def _load_skill_context(name: str, *, task_id: str | None = None) -> str:
    """Load one exact skill through Hermes' supported skill loader."""
    from tools.skills_tool import skill_view

    response = (
        skill_view(name=name, task_id=task_id)
        if task_id is not None
        else skill_view(name=name)
    )
    payload = json.loads(response) if isinstance(response, str) else response
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError("skill loader rejected recommendation")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("skill loader returned no content")
    return content


def register(ctx):
    default_steps = int(ctx.get_config("computer_max_steps", default=100))
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            name="switchyard",
            help="Configure Hermes Switchyard Jev access",
            setup_fn=_setup_cli,
            handler_fn=_cli_handler,
        )
    if hasattr(ctx, "register_auxiliary_task"):
        ctx.register_auxiliary_task(
            "hermes_switchyard_writer",
            display_name="Jev Text Writer",
            description="Compose field text for Jev computer use from public or sanitized state only.",
            defaults={"timeout": 30},
        )

    def _route() -> tuple[str, str, str]:
        missing = object()
        configured_provider = ctx.get_config("jev_provider", default=missing)
        configured_endpoint_value = ctx.get_config("api_endpoint", default=missing)
        provider_explicit = configured_provider is not missing
        endpoint_explicit = configured_endpoint_value is not missing
        provider = "auto" if not provider_explicit else configured_provider
        configured_endpoint = DEFAULT_ENDPOINT if not endpoint_explicit else configured_endpoint_value
        configured_model = ctx.get_config("jev_model", default=None)
        if provider not in {"auto", "typesafe", "openrouter"}:
            raise ValueError("jev_provider must be auto, typesafe, or openrouter")
        if configured_endpoint not in {DEFAULT_ENDPOINT, TYPESAFE_ENDPOINT}:
            raise ValueError("api_endpoint must be one of the fixed Jev endpoints")
        if provider_explicit and endpoint_explicit and (
            (provider == "openrouter" and configured_endpoint == TYPESAFE_ENDPOINT)
            or (provider == "typesafe" and configured_endpoint == DEFAULT_ENDPOINT)
        ):
            raise ValueError("jev_provider and api_endpoint select different providers")
        if configured_endpoint != DEFAULT_ENDPOINT:
            provider = "typesafe"
        elif provider == "auto":
            try:
                provider = "typesafe" if _secret("typesafe") else "openrouter"
            except Exception:
                provider = "openrouter"
        endpoint = TYPESAFE_ENDPOINT if provider == "typesafe" else DEFAULT_ENDPOINT
        model = configured_model or ("jev-latest" if provider == "typesafe" else "typesafe/jev-1.13")
        return endpoint, model, provider

    def client():
        endpoint, model, provider = _route()
        return DecisionClient(api_key=_secret(provider), endpoint=endpoint, model=model)

    def with_client(operation):
        active_client = client()
        try:
            return operation(active_client)
        finally:
            active_client.close()

    def decision_tools_available():
        try:
            _route()
            return True
        except Exception:  # noqa: BLE001 -- invalid route config stays hidden
            return False

    def computer_route_available():
        # Catalog visibility matches the native computer-use surface. Jev
        # credentials are required at call time, not to advertise the tool.
        return sys.platform in {"win32", "darwin", "linux"}

    def cache_identity():
        """Return live route scope with only a one-way credential generation."""
        endpoint, model, provider = _route()
        credential = _secret(provider)
        credential_sha256 = hashlib.sha256(
            f"hermes-switchyard:{provider}:".encode("utf-8")
            + credential.encode("utf-8")
        ).hexdigest()
        return {
            "provider": provider,
            "endpoint": endpoint,
            "model": model,
            "profile": os.environ.get("HERMES_PROFILE", "default"),
            "credential_sha256": credential_sha256,
        }

    def setting_bool(key, default):
        value = ctx.get_config(key, default=default)
        return value if type(value) is bool else default

    standing_ack = setting_bool("public_or_sanitized_data_ack", True)

    configured_routing_mode = ctx.get_config("automatic_skill_routing_mode", default=None)
    if configured_routing_mode is None:
        # The plugin owns its standalone scan and standing acknowledgement. A
        # future Hermes envelope can narrow the payload, but is not required.
        configured_routing_mode = (
            "hosted_sanitized"
            if setting_bool("automatic_skill_jev", True)
            else "local_only"
        )
    automatic_hook = build_pre_llm_call_hook(
        enabled=setting_bool("automatic_skill_recommendation", True),
        configured_candidates=ctx.get_config("automatic_skill_candidates", default=[]),
        routing_mode=configured_routing_mode,
        hosted_mode=ctx.get_config("automatic_skill_jev_mode", default="always"),
        public_or_sanitized_data_ack=setting_bool(
            "automatic_skill_public_or_sanitized_data_ack", False
        ),
        client_factory=client,
        cache_identity=cache_identity,
        local_threshold=_config_float(
            ctx.get_config("automatic_skill_local_threshold", default=0.20),
            0.20,
            minimum=0.0,
            maximum=1.0,
        ),
        local_margin=_config_float(
            ctx.get_config("automatic_skill_local_margin", default=0.05),
            0.05,
            minimum=0.0,
            maximum=1.0,
        ),
        cache_seconds=_config_float(
            ctx.get_config("automatic_skill_cache_seconds", default=30.0),
            30.0,
            minimum=0.0,
            maximum=300.0,
        ),
        consumer_mode=ctx.get_config(
            "automatic_skill_consumer_mode", default="advisory"
        ),
        skill_loader=_load_skill_context,
        mandatory_skills=discover_mandatory_skills(
            ctx.get_config("automatic_skill_mandatory_skills", default=[])
        ),
    )
    _publish_runtime_status(
        routing_mode=configured_routing_mode,
        consumer_mode=ctx.get_config("automatic_skill_consumer_mode", default="advisory"),
        public_or_sanitized_data_ack=setting_bool(
            "automatic_skill_public_or_sanitized_data_ack", False
        ),
        automatic_skill_jev_mode=ctx.get_config("automatic_skill_jev_mode", default="always"),
    )
    if automatic_hook is not None and hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", automatic_hook)

    def assess_handler(args, **kwargs):
        try:
            _require_public_data_ack(args, standing=standing_ack)
            state = args.get("state")
            questions = args.get("questions")
            if not isinstance(questions, dict) or not questions:
                raise ValueError("questions must be a non-empty object")
            def assess(active_client):
                with request_budget_scope(
                    active_client,
                    MAX_DECISION_REQUESTS,
                    deadline_seconds=args.get("deadline_seconds", DEFAULT_OPERATION_DEADLINE_SECONDS),
                ):
                    return active_client.decide(
                        state,
                        questions,
                        public_or_sanitized_data_ack=_resolved_public_data_ack(args, standing_ack),
                    )
            return json.dumps(with_client(assess))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def computer_handler(args, **kwargs):
        try:
            _require_public_data_ack(args, standing=standing_ack)
            ack = _resolved_public_data_ack(args, standing_ack)
            start_url = browser_use.requested_web_start(args.get("start_url"), args.get("goal") or "")
            if start_url:
                def run_dom(active_client):
                    return browser_use.run_browser_goal(
                        goal=args.get("goal") or "",
                        start_url=start_url,
                        client=active_client,
                        max_steps=int(args.get("max_steps") or default_steps),
                        min_actions_before_done=int(args.get("min_actions_before_done") or 0),
                        public_or_sanitized_data_ack=ack,
                        deadline_seconds=args.get("deadline_seconds", DEFAULT_OPERATION_DEADLINE_SECONDS),
                    )
                return json.dumps(with_client(run_dom))
            def native_dispatch(tool_name, tool_args):
                return ctx.dispatch_tool(tool_name, tool_args, **kwargs)
            result = with_client(lambda active_client: run_computer_goal(
                goal=args.get("goal") or "",
                app=args.get("app") or "",
                max_steps=int(args.get("max_steps") or default_steps),
                min_actions_before_done=int(args.get("min_actions_before_done") or 0),
                dispatch=native_dispatch,
                client=active_client,
                text_inputs=args.get("text_inputs"),
                allowed_hotkeys=args.get("allowed_hotkeys"),
                public_or_sanitized_data_ack=ack,
                deadline_seconds=args.get("deadline_seconds", DEFAULT_OPERATION_DEADLINE_SECONDS),
            ))
            return json.dumps(result)
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def skill_handler(args, **kwargs):
        try:
            _require_public_data_ack(args, standing=standing_ack)
            return json.dumps(with_client(lambda active_client: select_skill(
                    task=str(args.get("task") or ""),
                    candidates=list(args.get("candidates") or []),
                    client=active_client,
                    choice_confidence_threshold=args.get("choice_confidence_threshold", schemas.DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD),
                    needs_skill_threshold=args.get("needs_skill_threshold", schemas.DEFAULT_SKILL_NEEDS_THRESHOLD),
                    winning_probability_threshold=args.get("winning_probability_threshold", schemas.DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD),
                    public_or_sanitized_data_ack=_resolved_public_data_ack(args, standing_ack),
                    deadline_seconds=args.get("deadline_seconds", DEFAULT_OPERATION_DEADLINE_SECONDS),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def multi_skill_handler(args, **kwargs):
        try:
            _require_public_data_ack(args, standing=standing_ack)
            return json.dumps(with_client(lambda active_client: select_skills(
                    task=str(args.get("task") or ""),
                    candidates=list(args.get("candidates") or []),
                    client=active_client,
                    selection_threshold=args.get("selection_threshold", schemas.DEFAULT_SKILL_NEEDS_THRESHOLD),
                    max_selections=args.get("max_selections"),
                    public_or_sanitized_data_ack=_resolved_public_data_ack(args, standing_ack),
                    deadline_seconds=args.get("deadline_seconds", DEFAULT_OPERATION_DEADLINE_SECONDS),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def route_handler(args, **kwargs):
        try:
            _require_public_data_ack(args, standing=standing_ack)
            return json.dumps(with_client(lambda active_client: route_model(
                    task=str(args.get("task") or ""),
                    candidates=list(args.get("candidates") or []),
                    requirements=dict(args.get("requirements") or {}),
                    client=active_client,
                    capability_fit_threshold=args.get("capability_fit_threshold", schemas.DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD),
                    public_or_sanitized_data_ack=_resolved_public_data_ack(args, standing_ack),
                    deadline_seconds=args.get("deadline_seconds", DEFAULT_OPERATION_DEADLINE_SECONDS),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    ctx.register_tool(
        name="jev_assess",
        toolset="hermes_switchyard",
        schema=schemas.ASSESS,
        handler=assess_handler,
        check_fn=decision_tools_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_computer_use",
        toolset="computer_use",
        schema=schemas.COMPUTER_USE,
        handler=computer_handler,
        check_fn=computer_route_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_skill_select",
        toolset="hermes_switchyard",
        schema=schemas.SKILL_SELECT,
        handler=skill_handler,
        check_fn=decision_tools_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_skill_select_many",
        toolset="hermes_switchyard",
        schema=schemas.MULTI_SKILL_SELECT,
        handler=multi_skill_handler,
        check_fn=decision_tools_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_model_route",
        toolset="hermes_switchyard",
        schema=schemas.MODEL_ROUTE,
        handler=route_handler,
        check_fn=decision_tools_available,
        emoji="⚡",
    )
    if hasattr(ctx, "register_skill"):
        ctx.register_skill(
            "hermes-switchyard-operations",
            Path(__file__).parent / "skills" / "hermes-switchyard-operations" / "SKILL.md",
        )
    if sys.platform in {"win32", "darwin", "linux"} and hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section(
            "hermes-switchyard.computer-use",
            "Jev computer use is registered by default on Windows, macOS, and Linux whenever the "
            "computer_use toolset is enabled. Prefer jev_computer_use for multi-step GUI goals. "
            "If the goal or start_url is a public https page, the plugin runs a DOM browser loop: "
            "one Jev request per step, page clicks, no Hermes computer_use between actions. "
            "Desktop apps without a URL still use Cua Driver. Standing public_or_sanitized_data_ack "
            "is on after install; omit the field. Pass false to refuse one call. That acknowledgement "
            "is not blanket egress authorization and does not override mandatory skills, the user's "
            "native/computer-use preference, or other required controls. DONE returns "
            "completion_candidate/verified=false. Use low-level computer_use for a single explicit "
            "atomic action or recovery.",
            position="after_memory",
            max_chars=900,
        )
