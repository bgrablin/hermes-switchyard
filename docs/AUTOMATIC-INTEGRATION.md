# Automatic skill recommendations

Automatic skill recommendations are implemented in `jev_decision/automatic.py` and registered through Hermes' `pre_llm_call` plugin hook. The standalone per-turn contract is implemented in `jev_decision/egress.py`.

The feature is advisory only:

- it recommends an exact skill identifier;
- it does not load a skill;
- it does not change the system prompt, toolset, active model, provider, credentials, or fallback policy;
- it uses local token matching for a local-only fallback;
- the product default is `hosted_sanitized`, but hosted construction requires an allowed host-owned per-turn egress decision;
- a persistent acknowledgement is compatibility metadata only and is never an automatic egress grant.

The current plugin manifest is version `0.4.1` and declares `pre_llm_call` in `provides_hooks`.

## Runtime flow

For each Hermes user turn, the plugin receives the normal `pre_llm_call` callback payload. The callback uses only:

- `user_message` for bounded local matching;
- Hermes' profile-scoped `tools.skills_tool.skills_list()` response as the default local candidate source;
- explicit configured candidates when `automatic_skill_candidates` is non-empty;
- the additive `turn_egress_policy` envelope when the host provides it.

The plugin never uses prior user or assistant messages, or the cached system prompt, as a hosted candidate catalog. The original task is bounded to 4,000 characters for local matching. A hosted call uses only the envelope's bounded `allowed_payload` and exact candidate identifiers. Candidate descriptions, conversation history, and full skill bodies stay local. A recommendation is returned to Hermes as ephemeral user-message context:

```text
Advisory skill recommendation: consider the exact skill identifier "..." if it fits this request. The plugin did not load it. Mandatory skills, explicit instructions, safety controls, and the user's preferences take precedence.
```

The sentence is model-visible context, not a separate status message. Hermes keeps the system prompt unchanged. The normal skill invocation path remains responsible for loading a skill and recording skill usage.

## Local recommendation path

Local matching is deterministic and does not call OpenRouter or Jev.

1. Candidate names and descriptions are validated without trimming identifiers.
2. An explicit `automatic_skill_candidates` list has no catalog-size cap. Each name is limited to 128 characters and each description to 1,000 characters.
3. With no explicit list, the hook reads the full Hermes active profile `skills_list()` registry, filters disabled/platform-ineligible skills, and ranks it locally.
4. Matching uses bounded token overlap with a small local stopword list.
5. The top candidate must meet the local threshold and beat the next candidate by the local margin. Otherwise the hook abstains.
6. Results are cached per plugin process. The default cache lifetime is 30 seconds; the configured maximum is 300 seconds.

The local threshold and margin are policy gates, not calibrated probabilities. A selection means only that the local deterministic matcher crossed the configured gates.

## Hosted Jev path

The automatic path has three explicit routing modes:

| Mode | Local matching | Hosted client construction |
| --- | --- | --- |
| `off` | disabled | never |
| `local_only` | enabled | never |
| `hosted_sanitized` | enabled as a safe fallback | only after an allowed per-turn envelope |

`hosted_sanitized` is the product default. It does not mean that every turn is eligible. The persistent `automatic_skill_public_or_sanitized_data_ack` value is retained only for compatibility and is never sufficient authorization.

The hosted request uses the selected fixed Jev endpoint with OpenRouter fallbacks disabled. Hosted failure or timeout is unavailable and may preserve a valid local recommendation; a valid hosted abstention remains abstention and does not fall back locally. Hosted metadata is retained only in the typed routing receipt and callback state; it is not a user-facing completion claim.

The standalone envelope contract is:

```json
{
  "version": 1,
  "decision": "allow",
  "data_class": "public",
  "reason_code": "host_policy_allowed",
  "allowed_payload": "bounded public or sanitized task text"
}
```

`decision` must be `allow`, `data_class` must be `public` or `sanitized`, and `allowed_payload` must be non-empty, control-safe, and at most 4,000 characters. `deny`, `unknown`, malformed, missing, or restricted data classes fail closed before `client_factory()` is called. The host remains responsible for classification, sanitization, authorization, and provider-retention decisions. The plugin validates the envelope and does not pretend to be a DLP engine.

When the envelope is allowed and `automatic_skill_jev_mode` is `always`, Jev is called even when local matching is confident. `uncertain_only` is an explicit latency-saving override. The automatic hosted state contains only the allowed payload and candidate identifiers. It contains no candidate descriptions, conversation history, or skill bodies. Large catalogs still use partition fan-out and recursive reduction; the provider's 255-option Choice limit is not a catalog limit.

A valid hosted abstention is terminal for that turn and does not fall back to the local winner. A transport or client failure is unavailable and may preserve a local recommendation. Automatic results expose redacted `routing_status` and `routing_reason` metadata; they never expose task text, policy payload, descriptions, history, skill bodies, provider exception text, or credentials.

### Hermes core seam

The current Hermes core callback payload does not carry this envelope. This plugin therefore implements and tests the complete standalone contract, but it cannot claim core integration closure. The additive core change required for host-owned enforcement is one keyword on the existing invocation:

```python
_invoke_hook(
    "pre_llm_call",
    # existing fields remain unchanged
    turn_egress_policy=typed_host_owned_envelope,
)
```

`plugins_dispatch` already forwards additive keyword fields to callbacks that accept `**kwargs`; older narrow callbacks remain compatible. Core must populate the envelope from its own per-turn classification/sanitization decision. The plugin must receive `None` or a malformed envelope when that decision is unavailable and remain closed.

## Routing receipts and diagnostics

Every automatic recommendation ends by creating one typed receipt. The terminal state is one of `local_selection`, `hosted_selection`, `hosted_abstention`, `hosted_failure_local_fallback`, `hosted_skipped`, or `cache_hit`. A cache-hit receipt reports no hosted attempt, request, latency, usage, or request ID for that attempt; the selected source remains the origin of the cached result.

The supported operator diagnostic command is:

```text
hermes jev-decision receipt --json
```

It prints the latest receipt retained by the plugin. The receipt contains stable source, selection, attempt, error/skip, model, request, latency, usage, candidate-count, and shortlist-policy fields. `verified` is always `false` and `advisory_only` is always `true`; a receipt never proves that a skill was loaded, a model changed, or a GUI action completed. If no attempt has produced a receipt, the command prints a structured `no_receipt` diagnostic and exits non-zero.

Receipts include the plugin version and an exact source SHA when a validated `SOURCE-MANIFEST.json` is present, such as in a release archive. Source checkouts without that release manifest use the explicit `unavailable` value rather than guessing from Git state. Task text, candidate descriptions, conversation history, credentials, local paths, and provider exception text are not serialized.

## Configuration

All settings are profile-scoped under `plugins.entries.hermes-switchyard.settings`:

| Key | Default | Effect |
| --- | ---: | --- |
| `automatic_skill_recommendation` | `true` | Register the automatic `pre_llm_call` hook. Set `false` to disable the feature. |
| `automatic_skill_candidates` | `[]` | Explicit list of strings or `{name, description}` objects. Empty means use the full Hermes active profile `skills_list()` registry. |
| `automatic_skill_local_threshold` | `0.20` | Minimum local token-overlap score. Clamped to `[0, 1]`. |
| `automatic_skill_local_margin` | `0.05` | Minimum gap between the top two local candidates. Clamped to `[0, 1]`. |
| `automatic_skill_cache_seconds` | `30.0` | Per-process recommendation cache lifetime. Clamped to `[0, 300]`. |
| `automatic_skill_routing_mode` | `hosted_sanitized` | `off`, `local_only`, or `hosted_sanitized`. Hosted mode still requires an allowed per-turn envelope. |
| `automatic_skill_jev` | `true` | Deprecated compatibility switch. When the new mode is unset, `false` maps to `local_only`; `true` is not authorization. |
| `automatic_skill_jev_mode` | `always` | Evaluate the full catalog on every allowed turn. `uncertain_only` is an explicit latency-saving override. |
| `automatic_skill_public_or_sanitized_data_ack` | `false` | Deprecated persistent acknowledgement. It never authorizes automatic egress or replaces the per-turn policy. |

Configuration is read when the plugin registers. Start a fresh Hermes process after changing these settings; an existing process may retain the previous hook and values.

## Privacy and account boundary

The fallback local path sends no automatic recommendation request to Jev. The current task still enters the user's selected Hermes model through the normal turn, so local matching is not a DLP control.

The automatic hosted path sends only the host-approved `allowed_payload` and exact candidate identifiers to the selected Jev endpoint. Candidate descriptions, conversation history, and full skill bodies remain local. A successful policy decision does not establish provider retention, residency, or zero-data-retention properties. Classification and sanitization must happen in the host before the envelope reaches the plugin.

The selected credential is separate from a Codex or ChatGPT subscription. Save either provider key through Switchyard's masked setup command; never put a key in a URL, shell history, config value, repository file, or issue report.

```text
hermes jev-decision setup --provider typesafe
# or: hermes jev-decision setup --provider openrouter
```

`hermes plugins list --enabled` is a metadata/readiness check; it must not print credentials. Profiles do not share secrets automatically.

## No automatic skill load or model switch

A recommendation does not call Hermes' skill loader. It does not create an `on_skill_lifecycle` event. The user or the assistant must use the normal skill command path if the skill is needed.

`jev_model_route` remains a separate advisory tool. Automatic recommendations do not select a new Hermes model, provider, account, credential, toolset, or fallback. A Jev fit signal is not a calibrated quality or safety claim, and the active model may encode cost, quota, capability, residency, or authorization policy.

## Decision primitives

`jev_assess` and the internal routing paths use Jev `Choice`, `Score`, and
`Noul` questions. Choice is for a closed set, Score is for an ordered rubric,
and Noul is for a yes/no proposition. Independent questions sharing one state are
packed into bounded requests and recombined; code owns thresholds, weights, and
side effects. Score answers are validated before returning and are not treated as
calibrated truth.

## Observable hook behavior

A successful local smoke can be run without a TypeSafe or OpenRouter account:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

If the current profile's `skills_list()` registry contains a matching skill, the model-bound current user message contains an advisory recommendation for `docker-management`. The phrase says that the plugin did not load the skill. There is no separate recommendation banner or automatic skill-use event. Hosted automatic routing remains gated by the host-owned per-turn envelope; the persistent acknowledgement alone does not authorize a provider call.

The local hook can abstain when the registry is empty, no candidate overlap exists, or the top match is ambiguous. Abstention is normal behavior, not a failed skill load.

To make the smoke deterministic, configure an explicit candidate list before starting a fresh process:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The config command parses list and mapping literals as YAML/JSON values. The list is still validated by the plugin and is not a permission grant.

For an allowed public/synthetic smoke, configure the account first, then use the explicit routing mode. The host must supply the per-turn envelope; a persistent acknowledgement alone is intentionally insufficient:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode hosted_sanitized
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_jev_mode always
```

Use only a task and configured metadata that are public or already sanitized. Start a fresh process, then run the public request through a host integration that supplies `turn_egress_policy`. If the envelope is absent, the automatic hosted path remains closed. A hosted failure may preserve a local selection; a valid hosted abstention remains abstention. No fallback provider or model is selected.

## Source anchors

- `jev_decision/automatic.py` — profile-scoped registry discovery, local ranking, routing modes, policy gating, redacted metadata, cache, receipts, and hook callback
- `jev_decision/egress.py` — standalone versioned per-turn envelope contract and fail-closed evaluator
- `jev_decision/receipt_state.py` — source identity, receipt contract validation, and plugin-owned diagnostic state
- `jev_decision/__init__.py` — plugin settings and `ctx.register_hook("pre_llm_call", ...)`
- `plugin.yaml` — manifest hook declaration and configuration defaults
- Hermes `tools/skills_tool.py` — public profile-scoped `skills_list()` response used for catalog discovery
- Hermes `agent/turn_context.py` — `pre_llm_call` timing and user-message context injection
- Hermes `hermes_cli/plugins_dispatch.py` — callback and timeout behavior
- Hermes `hermes_cli/config.py` — scalar and YAML/JSON config value parsing
