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
