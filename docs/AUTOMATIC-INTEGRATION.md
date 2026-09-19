# Automatic skill recommendations

Automatic skill recommendations are implemented in `jev_decision/automatic.py` and registered through Hermes' `pre_llm_call` plugin hook.

The feature is advisory only:

- it recommends an exact skill identifier;
- it does not load a skill;
- it does not change the system prompt, toolset, active model, provider, credentials, or fallback policy;
- it uses local token matching by default;
- hosted Jev is disabled by default and requires a separate configuration switch plus a public/sanitized-data attestation.

The current plugin manifest is version `0.3.2` and declares `pre_llm_call` in `provides_hooks`.

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
2. An explicit `automatic_skill_candidates` list is limited to 32 entries. Each name is limited to 128 characters and each description to 1,000 characters.
3. With no explicit list, the hook calls Hermes' active profile `skills_list()` registry, which filters disabled/platform-ineligible skills, accepts at most 255 records, and ranks the top 32.
4. Matching uses bounded token overlap with a small local stopword list.
5. The top candidate must meet the local threshold and beat the next candidate by the local margin. Otherwise the hook abstains.
6. Results are cached per plugin process. The default cache lifetime is 30 seconds; the configured maximum is 300 seconds.

The local threshold and margin are policy gates, not calibrated probabilities. A selection means only that the local deterministic matcher crossed the configured gates.

## Hosted Jev path

The hosted path runs only when all of these are true:

- `automatic_skill_jev: true`;
- `automatic_skill_public_or_sanitized_data_ack: true`;
- the plugin can obtain its OpenRouter credential through Hermes' scoped secret flow.

Hosted Jev receives the bounded current task and exact candidate identifiers only. Descriptions remain local ranking metadata for both registry-discovered and explicitly configured candidates; conversation history is not sent.

The hosted request uses the plugin's fixed Decisions endpoint and configured Jev model. The request sets `allow_fallbacks: false`. Hosted failure or timeout is unavailable and may preserve a valid local recommendation; a valid hosted abstention is preserved as abstention and does not fall back locally. Hosted metadata may be retained in the callback's internal `last_result` for diagnostics, but it is not a user-facing completion claim.

The attestation is not DLP, authorization, or a privacy guarantee. Do not enable hosted Jev for private, employer, regulated, credential, payment, verification, or otherwise restricted content. Do not infer a retention or zero-data-retention property from a successful request.

## Configuration

All settings are profile-scoped under `plugins.entries.jev-decision.settings`:

| Key | Default | Effect |
| --- | ---: | --- |
| `automatic_skill_recommendation` | `true` | Register the automatic `pre_llm_call` hook. Set `false` to disable the feature. |
| `automatic_skill_candidates` | `[]` | Explicit list of strings or `{name, description}` objects. Empty means use Hermes' active profile `skills_list()` registry. Descriptions stay local. |
| `automatic_skill_local_threshold` | `0.20` | Minimum local token-overlap score. Clamped to `[0, 1]`. |
| `automatic_skill_local_margin` | `0.05` | Minimum gap between the top two local candidates. Clamped to `[0, 1]`. |
| `automatic_skill_cache_seconds` | `30.0` | Per-process recommendation cache lifetime. Clamped to `[0, 300]`. |
| `automatic_skill_jev` | `false` | Permit hosted Jev decisions for automatic recommendations. |
| `automatic_skill_public_or_sanitized_data_ack` | `false` | Persistent operator attestation required before the bounded current task and exact candidate identifiers reach hosted Jev; descriptions and history are excluded. |

Configuration is read when the plugin registers. Start a fresh Hermes process after changing these settings; an existing process may retain the previous hook and values.

## Privacy and account boundary

The local path sends no automatic recommendation request to Jev. The current task still enters the user's selected Hermes model through the normal turn, so local matching is not a DLP control.

The hosted path sends the bounded current user task and exact candidate identifiers to the fixed OpenRouter Decisions route. Candidate descriptions and conversation history remain local. This narrower payload does not make the request safe for restricted data.

The OpenRouter credential is separate from a Codex or ChatGPT subscription. Codex/ChatGPT subscription billing does not pay for OpenRouter requests. The plugin manifest declares `OPENROUTER_API_KEY` as a required environment secret. Use the manifest's masked profile-install flow, not the provider-pool command and not a command-line key:

```text
hermes plugins install bgrablin/hermes-switchyard --force --enable
```

Hermes prompts for the key through its masked secret UI and saves it in the active profile's `.env`. The `--force` flag reruns the prompt when the plugin is already installed. Profiles do not share this secret automatically; start a fresh Hermes process after adding or changing it. Never put the key in a URL, shell history, config value, repository file, or issue report.

`hermes plugins list --enabled` is a metadata/readiness check; it must not print the key. Direct TypeSafe account access is not a supported setup path for this plugin.

## No automatic skill load or model switch

A recommendation does not call Hermes' skill loader. It does not create an `on_skill_lifecycle` event. The user or the assistant must use the normal skill command path if the skill is needed.

`jev_model_route` remains a separate advisory tool. Automatic recommendations do not select a new Hermes model, provider, account, credential, toolset, or fallback. A Jev fit signal is not a calibrated quality or safety claim, and the active model may encode cost, quota, capability, residency, or authorization policy.

## Decision primitives

The current plugin uses Jev `Choice` questions for closed candidate selection and
`Noul` questions for yes/no need and fit gates. TypeSafe `Score` is intentionally
unsupported: no current tool or hook consumer defines a numeric score contract,
threshold semantics, calibration evidence, or a safe downstream action for one.
The client rejects unsupported question types instead of accepting an untyped
number. Adding Score later would require a separately reviewed schema, policy
thresholds, calibrated evaluation, and a caller contract; this is not a claim
that Jev cannot provide Score.

## Observable hook behavior

A successful local smoke can be run without an OpenRouter account:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

If the current profile's `skills_list()` registry contains a matching skill, the model-bound current user message contains an advisory recommendation for `docker-management`. The phrase says that the plugin did not load the skill. There is no separate recommendation banner, no automatic skill-use event, and no provider call in the default local mode.

The local hook can abstain when the registry is empty, no candidate overlap exists, or the top match is ambiguous. Abstention is normal behavior, not a failed skill load.

To make the smoke deterministic, configure an explicit candidate list before starting a fresh process:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The config command parses list and mapping literals as YAML/JSON values. The list is still validated by the plugin and is not a permission grant.

For a hosted public/synthetic smoke, configure the account first, then explicitly enable the hosted path:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_jev true
hermes config set plugins.entries.jev-decision.settings.automatic_skill_public_or_sanitized_data_ack true
```

Use only a task and configured metadata that are public or already sanitized. Start a fresh process, then run the same public request. A hosted failure or timeout may preserve a local selection; a valid hosted abstention remains abstention. No fallback provider or model is selected.

## Source anchors

- `jev_decision/automatic.py` — profile-scoped registry discovery, local ranking, hosted gating, cache, and hook callback
- `jev_decision/__init__.py` — plugin settings and `ctx.register_hook("pre_llm_call", ...)`
- `plugin.yaml` — manifest hook declaration and configuration defaults
- Hermes `tools/skills_tool.py` — public profile-scoped `skills_list()` response used for catalog discovery
- Hermes `agent/turn_context.py` — `pre_llm_call` timing and user-message context injection
- Hermes `hermes_cli/plugins_dispatch.py` — callback and timeout behavior
- Hermes `hermes_cli/config.py` — scalar and YAML/JSON config value parsing
