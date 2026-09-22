# Approved model routing

`jev_model_route_approved` evaluates only the active profile's locally configured, explicitly approved model registry. It is an advisory routing point: the result never changes the active Hermes model, provider, account, configuration, or fallback chain.

## Why this exists

The general `jev_model_route` tool accepts a caller-supplied candidate set. That is useful for bounded analysis, but it is not an authorization boundary. The approved-registry tool separates policy from model judgment:

1. The operator owns provider, model, account, authorization, data-class, capability, context, cost, version, and expiry metadata.
2. Code validates and filters that metadata locally.
3. Jev supplies only bounded capability-fit judgments for eligible candidates.
4. Code chooses the lowest-cost qualified candidate.
5. Hermes receives a typed recommendation with `applied: false`.

Jev never receives credentials or account secrets. It cannot invent authorization metadata, add candidates, switch the coordinator model, or select a fallback provider.

## Registry settings

The following settings are profile-scoped under `plugins.entries.hermes-switchyard.settings`:

- `approved_model_registry`: list of model records.
- `approved_model_registry_version`: non-empty operator-managed version.
- `approved_model_registry_valid_until`: timezone-aware ISO-8601 expiry.

Each model record has:

```json
{
  "id": "routine-worker",
  "provider": "openai-codex",
  "model": "gpt-5.6-luna-900k",
  "account": "included",
  "approved": true,
  "data_classes_allowed": ["public"],
  "tool_capabilities": ["terminal"],
  "context_limit": 900000,
  "cost": 0.0,
  "description": "Approved routine worker"
}
```

`provider`, `model`, and `account` are returned only as local recommendation metadata. They are not sent as policy facts for Jev to infer or change.

An empty, malformed, wholly unapproved, or expired registry makes no provider call. The result is `invalid_registry` or `stale_registry` with a stable local reason code.

## Tool contract

The caller supplies only:

- public or sanitized task text;
- explicit requirements for data classes, tools, context, and budget;
- an optional bounded capability-fit threshold;
- `public_or_sanitized_data_ack: true`.

Example model-facing call shape:

```json
{
  "task": "Choose an approved model for this public terminal task",
  "requirements": {
    "data_classes": ["public"],
    "tool_capabilities": ["terminal"],
    "context_limit": 32000,
    "budget": 1.0
  },
  "public_or_sanitized_data_ack": true
}
```

The typed response distinguishes:

- `selected`: recommendation available;
- `abstained`: no candidate met the Jev fit threshold;
- `budget_exhausted`: candidates were rejected by local budget policy;
- `stale_registry`: registry expiry passed;
- `invalid_registry`: local policy metadata is invalid;
- `provider_unavailable`: Jev transport failed;
- `invalid_response`: Jev selected an identifier absent from the registry.

A selected response includes the exact registry ID and local provider/model/account projection. It always includes `applied: false` and the policy text `active Hermes route is unchanged`.

## Applying a recommendation

Switchyard does not apply it. The operator or coordinator must use Hermes' normal explicit model-selection workflow and its account, quota, and data controls. A recommendation grants no new provider, model, account, credential, or paid-fallback authority.

## Verification boundary

Offline tests prove registry validation, expiry, cheapest-qualified selection, provider-failure classification, and no automatic switch. They do not prove that a recommended model can complete an arbitrary task or that its account currently has quota.

## Hermes model-selection apply seam

### Checked upstream status

The reviewed Hermes Agent pin is commit `8503ee4459316ce092b5d69b7d396c27aa03d0be`. At that commit, `PluginContext` exposes registration for tools, hooks, middleware, skills, and providers, but no public model-selection registration or apply callback. Hermes has an internal `apply_model_selection` configuration helper for callers that already own the model-switch workflow. That helper is not a plugin seam. The checked upstream commit therefore has no model-selection apply seam for Switchyard to use.

### Required host contract

Switchyard needs a stable Hermes core API that can:

1. enumerate the selectable model routes and the host-owned provider, account, quota, data-class, and capability constraints;
2. apply one validated route through the normal Hermes model-selection path, with an explicit scope such as the next turn or the active session; and
3. report the applied route and the active-route readback, or a typed refusal, without changing credentials or inventing a fallback.

### Current Switchyard behavior

Switchyard keeps model routing recommend-only. It validates the approved registry locally, asks Jev for bounded capability fit, returns a typed receipt with `applied: false`, and leaves the active Hermes model unchanged. `register_model_route_adapter` probes known names for a future seam and records a safe no-op when none is present. `accept_model_route` refuses to apply a recommendation unless the host supplies an explicit apply callback. No silent model change or fallback is allowed.

### Behavior after the seam exists

After Hermes exposes the contract, Switchyard can register the supported callback, retain its local registry and policy checks, and pass only an explicitly accepted route to the host apply operation. It must set `applied: true` only after Hermes reports success and the active-route readback matches. A missing, rejected, or ambiguous host result remains `applied: false` with a typed reason. The recommendation and account boundary stay unchanged.

## Policy-owned adapter (0.5.0)

Hermes Agent **0.19** does not expose a plugin hook or `PluginContext` API that can change the active coordinator model. Switchyard therefore ships a policy-owned adapter that makes approved-registry routing mechanical without silently swapping models:

| Surface | Role |
| --- | --- |
| `jev_model_route` | Tool routing point for caller-supplied candidates |
| `jev_model_route_approved` | Tool routing point for the profile-owned approved registry |
| `hermes_switchyard.model_registry.route_model_from_registry` | Code-owned registry helper |
| `hermes_switchyard.model_route_adapter.recommend_model_route` | **First-class** coordinator entry: registry path + typed receipt |
| `hermes_switchyard.model_route_adapter.recommend_model_route_from_profile` | Profile-registry entry + typed receipt |
| `hermes_switchyard.model_route_adapter.register_model_route_adapter` | Probes Hermes for a future model-selection seam; **safe no-op** on 0.19 |
| `hermes_switchyard.model_route_adapter.accept_model_route` | Explicit accept path; refuses unless the host supplies an apply callback |

### Receipt contract

Every adapter recommendation includes:

- `applied: false` — the active Hermes model is unchanged
- `no_fallback: true` — provider/model fallback is never invented
- `account_boundary` — recommendation grants no new account or credential authority
- `source` — `code_owned_registry` or `profile_owned_registry`
- `integration_point` — the callable coordinators should invoke
- Distinct `status` / `abstention_reason` values: `selected`, `abstained`, `empty_registry`, `stale_registry`, `no_eligible_candidates`, `budget_exhausted` / `invalid_registry` / `provider_unavailable` (profile path)

### Registration behavior on Hermes 0.19

On plugin `register()`, Switchyard calls `register_model_route_adapter(ctx)`:

1. Probe for `register_model_router` / `register_model_selection_policy` / `register_model_route`, or a known model-selection hook name.
2. If absent (Hermes 0.19), record `mode: noop_seam_unavailable` and leave the runtime model untouched.
3. If a future host exposes a supported method, register a **recommend-only** callback. Apply still requires `accept_model_route(..., apply_callback=...)`.

`hermes switchyard status --json` includes `model_route_adapter` so operators can see the registration receipt.

### Coordinator integration (mechanical)

```python
from hermes_switchyard.model_route_adapter import (
    recommend_model_route,
    accept_model_route,
)

receipt = recommend_model_route(
    task=public_task,
    requirements={
        "data_classes": ["public"],
        "tool_capabilities": ["terminal"],
        "context_limit": 32000,
        "budget": 1.0,
    },
    client=jev_client,
    public_or_sanitized_data_ack=True,
    registry=approved_candidates,  # code-owned; descriptions never confer approval
)
# receipt["applied"] is False. Use Hermes' normal explicit model workflow to change
# models, or pass an explicit apply_callback once a Hermes apply seam exists:
# accept_model_route(receipt, apply_callback=hermes_supported_apply)
```

### Honesty boundary

Closing issue #11 on Hermes 0.19 means shipping the strongest adapter + docs + tests that make integration mechanical and fail-closed. It does **not** mean Switchyard can change the active Hermes model; that requires a Hermes core apply seam that does not exist in 0.19.
