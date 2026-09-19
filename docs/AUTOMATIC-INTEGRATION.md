# Automatic skill recommendations

Automatic skill recommendations are implemented in `jev_decision/automatic.py` and registered through Hermes' `pre_llm_call` plugin hook.

The feature is advisory only:

- it recommends an exact skill identifier;
- it does not load a skill;
- it does not change the system prompt, toolset, active model, provider, credentials, or fallback policy;
- it uses local token matching by default;
- hosted Jev is disabled by default and requires a separate configuration switch plus a public/sanitized-data attestation.

The current plugin manifest is version `0.4.1` and declares `pre_llm_call` in `provides_hooks`.

## Runtime flow

For each Hermes user turn, the plugin receives the normal `pre_llm_call` callback payload. The callback uses only:

- `user_message` as the bounded task text;
- Hermes' profile-scoped `tools.skills_tool.skills_list()` response as the default candidate source;
- explicit configured candidates when `automatic_skill_candidates` is non-empty.

It does not use prior user or assistant messages, or the cached system prompt, as a candidate catalog. The task text is bounded to 4,000 characters. A recommendation is returned to Hermes as ephemeral user-message context:

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

The hosted path runs only when all of these are true:

- `automatic_skill_jev: true`;
- `automatic_skill_public_or_sanitized_data_ack: true`;
- the plugin can obtain a TypeSafe or OpenRouter credential through Hermes' scoped secret flow.
- mode is `always` (the default), or local matching abstained under the explicit `uncertain_only` override.

Hosted Jev receives the bounded current task plus exact candidate identifiers and bounded descriptions. Conversation history and full skill bodies stay local. Large catalogs use partition fan-out and recursive reduction; the provider's 255-option Choice limit is not a catalog limit.

The hosted request uses the selected fixed Jev endpoint with OpenRouter fallbacks disabled. Hosted failure or timeout is unavailable and may preserve a valid local recommendation; a valid hosted abstention remains abstention and does not fall back locally. Hosted metadata is retained only in the typed routing receipt and callback state; it is not a user-facing completion claim.

The attestation is not DLP, authorization, or a privacy guarantee. Do not enable hosted Jev for private, employer, regulated, credential, payment, verification, or otherwise restricted content. Do not infer a retention or zero-data-retention property from a successful request.

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
| `automatic_skill_jev` | `true` | Prefer hosted Jev decisions after the separate public/sanitized-data attestation is enabled. |
| `automatic_skill_jev_mode` | `always` | Evaluate the full catalog on every eligible turn. `uncertain_only` is an explicit latency-saving override. |
| `automatic_skill_public_or_sanitized_data_ack` | `false` | Persistent operator attestation required before the bounded task, identifiers, and descriptions reach hosted Jev; descriptions and history are not sent when hosted is off. |

Configuration is read when the plugin registers. Start a fresh Hermes process after changing these settings; an existing process may retain the previous hook and values.

## Privacy and account boundary

The fallback local path sends no automatic recommendation request to Jev. The current task still enters the user's selected Hermes model through the normal turn, so local matching is not a DLP control.

The hosted path sends the bounded current user task, exact candidate identifiers, and bounded descriptions to the selected Jev endpoint. Conversation history and full skill bodies remain local. This does not make restricted data safe.

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

If the current profile's `skills_list()` registry contains a matching skill, the model-bound current user message contains an advisory recommendation for `docker-management`. The phrase says that the plugin did not load the skill. There is no separate recommendation banner or automatic skill-use event. A provider call remains gated by the public/sanitized-data attestation.

The local hook can abstain when the registry is empty, no candidate overlap exists, or the top match is ambiguous. Abstention is normal behavior, not a failed skill load.

To make the smoke deterministic, configure an explicit candidate list before starting a fresh process:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The config command parses list and mapping literals as YAML/JSON values. The list is still validated by the plugin and is not a permission grant.

For a hosted public/synthetic smoke, configure the account first, then explicitly enable the hosted path:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_jev true
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack true
```

Use only a task and configured metadata that are public or already sanitized. Start a fresh process, then run the same public request. A hosted failure or timeout may preserve a local selection; a valid hosted abstention remains abstention. No fallback provider or model is selected.

## Source anchors

- `jev_decision/automatic.py` — profile-scoped registry discovery, local ranking, hosted gating, cache, receipts, and hook callback
- `jev_decision/receipt_state.py` — source identity, receipt contract validation, and plugin-owned diagnostic state
- `jev_decision/__init__.py` — plugin settings and `ctx.register_hook("pre_llm_call", ...)`
- `plugin.yaml` — manifest hook declaration and configuration defaults
- Hermes `tools/skills_tool.py` — public profile-scoped `skills_list()` response used for catalog discovery
- Hermes `agent/turn_context.py` — `pre_llm_call` timing and user-message context injection
- Hermes `hermes_cli/plugins_dispatch.py` — callback and timeout behavior
- Hermes `hermes_cli/config.py` — scalar and YAML/JSON config value parsing
