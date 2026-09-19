"""Jev structured-decision plugin for Hermes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import receipt_state, schemas
from .automatic import _config_float, build_pre_llm_call_hook
from .client import DEFAULT_ENDPOINT, DEFAULT_OPERATION_DEADLINE_SECONDS, TYPESAFE_ENDPOINT, DecisionClient
from .computer_use import StaleTargetError, run_computer_goal
from .routing import route_model, select_skill, select_skills


def _cli_handler(args):
    command = getattr(args, "switchyard_command", None) or getattr(args, "jev_command", None)
    if command == "status":
        payload = {"plugin": "hermes-switchyard", "status": "installed", "network": False}
        print(json.dumps(payload, sort_keys=True) if getattr(args, "json_output", False) else "Hermes Switchyard: installed (local status only)")
        return 0
    if command == "guide":
        print("Hermes Switchyard uses Jev for bounded decisions. Use setup to save a provider key; use test --live only when a billed request is intended.")
        return 0
    if command == "test":
        if getattr(args, "live", False) is not True or getattr(args, "public_or_sanitized_data_ack", False) is not True:
            print("Refusing live test: pass --live and --public-or-sanitized-data-ack.")
            return 2
        return 0 if _secret(getattr(args, "provider", "auto")) else 1
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


def _error(exc):
    # Preserve observability with stable local codes, never arbitrary provider,
    # executor, UI, or credential text.
    if isinstance(exc, PermissionError):
        code, reason = "ack_required", "public_or_sanitized_data_ack is required"
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
    return json.dumps({"status": "error", "error": {"code": code, "reason": reason}})


def _require_public_data_ack(args):
    if args.get("public_or_sanitized_data_ack", False) is not True:
        raise PermissionError(
            "public_or_sanitized_data_ack must be true: caller attestation only, not DLP"
        )


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

    def route_available():
        try:
            endpoint, model, provider = _route()
            key = _secret(provider)
            if not key:
                return False
            probe = DecisionClient(api_key=key, endpoint=endpoint, model=model)
            probe.close()
            return True
        except Exception:  # noqa: BLE001 -- unavailable routes stay hidden
            return False

    def computer_route_available():
        return sys.platform in {"win32", "darwin", "linux"} and route_available()

    def setting_bool(key, default):
        value = ctx.get_config(key, default=default)
        return value if type(value) is bool else default

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
        cache_identity=lambda: {
            "provider": ctx.get_config("jev_provider", default="auto"),
            "endpoint": ctx.get_config("api_endpoint", default=DEFAULT_ENDPOINT),
            "model": ctx.get_config("jev_model", default=None),
        },
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
    )
    if automatic_hook is not None and hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", automatic_hook)

    def assess_handler(args, **kwargs):
        try:
            _require_public_data_ack(args)
            state = args.get("state")
            questions = args.get("questions")
            if not isinstance(questions, dict) or not questions:
                raise ValueError("questions must be a non-empty object")
            return json.dumps(with_client(lambda active_client: active_client.decide(
                    state,
                    questions,
                    public_or_sanitized_data_ack=args.get("public_or_sanitized_data_ack", False),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def computer_handler(args, **kwargs):
        try:
            _require_public_data_ack(args)
            result = with_client(lambda active_client: run_computer_goal(
                goal=args.get("goal") or "",
                app=args.get("app") or "",
                max_steps=int(args.get("max_steps") or default_steps),
                min_actions_before_done=int(args.get("min_actions_before_done") or 0),
                dispatch=ctx.dispatch_tool,
                client=active_client,
                text_inputs=args.get("text_inputs"),
                allowed_hotkeys=args.get("allowed_hotkeys"),
                public_or_sanitized_data_ack=args.get("public_or_sanitized_data_ack", False),
            ))
            return json.dumps(result)
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def skill_handler(args, **kwargs):
        try:
            _require_public_data_ack(args)
            return json.dumps(with_client(lambda active_client: select_skill(
                    task=str(args.get("task") or ""),
                    candidates=list(args.get("candidates") or []),
                    client=active_client,
                    choice_confidence_threshold=args.get("choice_confidence_threshold", schemas.DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD),
                    needs_skill_threshold=args.get("needs_skill_threshold", schemas.DEFAULT_SKILL_NEEDS_THRESHOLD),
                    winning_probability_threshold=args.get("winning_probability_threshold", schemas.DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD),
                    public_or_sanitized_data_ack=args.get("public_or_sanitized_data_ack", False),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def multi_skill_handler(args, **kwargs):
        try:
            _require_public_data_ack(args)
            return json.dumps(with_client(lambda active_client: select_skills(
                    task=str(args.get("task") or ""),
                    candidates=list(args.get("candidates") or []),
                    client=active_client,
                    selection_threshold=args.get("selection_threshold", schemas.DEFAULT_SKILL_NEEDS_THRESHOLD),
                    max_selections=args.get("max_selections"),
                    public_or_sanitized_data_ack=args.get("public_or_sanitized_data_ack", False),
                    deadline_seconds=args.get("deadline_seconds", schemas.DEFAULT_OPERATION_DEADLINE_SECONDS),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    def route_handler(args, **kwargs):
        try:
            _require_public_data_ack(args)
            return json.dumps(with_client(lambda active_client: route_model(
                    task=str(args.get("task") or ""),
                    candidates=list(args.get("candidates") or []),
                    requirements=dict(args.get("requirements") or {}),
                    client=active_client,
                    capability_fit_threshold=args.get("capability_fit_threshold", schemas.DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD),
                    public_or_sanitized_data_ack=args.get("public_or_sanitized_data_ack", False),
                )))
        except Exception as exc:  # noqa: BLE001 -- tool handlers return structured errors
            return _error(exc)

    ctx.register_tool(
        name="jev_assess",
        toolset="hermes_switchyard",
        schema=schemas.ASSESS,
        handler=assess_handler,
        check_fn=route_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_computer_use",
        toolset="hermes_switchyard",
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
        check_fn=route_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_skill_select_many",
        toolset="hermes_switchyard",
        schema=schemas.MULTI_SKILL_SELECT,
        handler=multi_skill_handler,
        check_fn=route_available,
        emoji="⚡",
    )
    ctx.register_tool(
        name="jev_model_route",
        toolset="hermes_switchyard",
        schema=schemas.MODEL_ROUTE,
        handler=route_handler,
        check_fn=route_available,
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
            "Jev computer use is a configurable capability for multi-step browser or native GUI goals on "
            "Windows, macOS, and Linux. Use it only when the caller explicitly approves the run and attests "
            "that all state is public or sanitized; that acknowledgement is not blanket egress authorization "
            "and does not override mandatory skills, the user's native/computer-use preference, or other required "
            "controls. The loop delegates to Hermes' Cua Driver-backed computer_use tool, keeps application-owned "
            "candidate IDs, re-captures before actions, and returns completion_candidate/verified=false. An "
            "independent coordinator-owned verifier remains required. Otherwise preserve the native computer-use "
            "workflow, and use low-level computer_use for a single explicit atomic action or recovery.",
            position="after_memory",
            max_chars=900,
        )
