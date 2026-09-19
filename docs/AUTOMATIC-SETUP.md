# Set up automatic skill recommendations

This guide enables the implemented automatic skill recommendation hook for the `hermes-switchyard` plugin.

The configuration default is `hosted_sanitized`. Hosted routing uses the plugin-owned standalone contract: persistent acknowledgement plus a strict local per-turn scan before client construction. A compatible Hermes host may optionally provide a narrower typed envelope. This guide does not claim that a recommendation certifies model quality or GUI completion.

## 1. Install the pinned plugin

Install an exact 40-character commit and enable the plugin:

```text
hermes plugins install bgrablin/hermes-switchyard --ref FULL_40_SHA --enable
hermes plugins list --enabled --plain
```

Replace `FULL_40_SHA` with the commit you intend to run. Do not put a credential in the repository URL or shell history. If you want installation and enablement as separate steps, use `--no-enable` and then:

```text
hermes plugins enable hermes-switchyard
```

Validate a checkout without changing a live profile:

```text
hermes plugins doctor . --ci
```

Plugin Doctor checks discovery, import, registration, declared hooks, and tools. It does not prove model quality, privacy, skill correctness, or GUI completion.

## 2. Start with an explicit local-only recommendation mode

Local recommendations are enabled by default. To keep all automatic hosted construction off:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_recommendation true
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
```

`local_only` never constructs a Jev client. The local matcher uses Hermes' active profile `skills_list()` registry. It searches the full registry locally and abstains when the score is too low or the winner is too close to the runner-up. A persistent acknowledgement does not change this mode.

For a controlled candidate list, set YAML/JSON as one shell argument:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The plugin validates candidate names and descriptions. This setting does not load or authorize the listed skills; it only supplies local ranking data.

## 3. Reload in a fresh Hermes process

The plugin reads these settings while it registers the hook. Exit the current Hermes process and start a new one after installation or configuration changes. A new turn in an old process is not sufficient proof that the new hook settings loaded.

Confirm the plugin remains enabled:

```text
hermes plugins list --enabled --plain
```

## 4. Run a public local smoke

No hosted Jev key is required for this local smoke:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

Expected behavior when `docker-management` is available to the session:

- the current user-message context contains an advisory recommendation for the exact identifier `docker-management`;
- the recommendation says that the plugin did not load the skill;
- the system prompt and active Hermes model are unchanged;
- no TypeSafe/OpenRouter Jev request is made while the attestation remains false;
- no skill-use event is created just because the recommendation was produced.

The recommendation is context sent to the model, not a separate status banner. The assistant may ignore it. If the profile registry is empty, the candidate list is ambiguous, or the request has no overlap, abstention is expected.

## 5. Configure hosted Jev only after a per-turn allow decision

Hosted Jev requires either a TypeSafe account/key or an OpenRouter account/key, plus available allowance. `jev_provider: auto` prefers direct TypeSafe. Codex or ChatGPT subscription billing does not pay for either route.

Set the explicit hosted mode and standing acknowledgement only when the task is public or already sanitized:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode hosted_sanitized
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_jev_mode always
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack true
```

An optional host envelope may narrow the payload. It is not required. If supplied, it must be a versioned envelope such as:

```json
{"version":1,"decision":"allow","data_class":"sanitized","reason_code":"host_policy_allowed","allowed_payload":"sanitized public task"}
```

The plugin scans the bounded task locally before construction. Only the accepted bounded task (or the narrower `allowed_payload`) and candidate identifiers are sent to Jev. Candidate descriptions, history, and full skill bodies stay local. False acknowledgement, restricted content, or an explicit denied/unknown/malformed envelope fails closed before client construction. `always` calls Jev even for a confident local match. Use `uncertain_only` only as an explicit latency-saving override. A valid hosted abstention stays abstained; only an unavailable transport may preserve a local recommendation.

## 6. Disable or roll back

Disable automatic recommendations while leaving the rest of the plugin enabled:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_recommendation false
```

Disable hosted requests but keep local matching:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
```

Remove the automatic settings and return to manifest defaults:

```text
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_candidates
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_local_threshold
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_local_margin
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_cache_seconds
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_jev
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_jev_mode
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_recommendation
```

Start a fresh process after disabling or unsetting values.

To disable the complete plugin:

```text
hermes plugins disable hermes-switchyard
```

To return to a previously reviewed plugin revision, reinstall that exact revision:

```text
hermes plugins remove hermes-switchyard
hermes plugins install bgrablin/hermes-switchyard --ref PREVIOUS_FULL_SHA --enable
```

Keep the previous 40-character SHA as the rollback target. Verify the installed revision with `hermes plugins list --plain` and run Plugin Doctor before using it.

## 7. Troubleshooting

`hermes plugins list --enabled --plain`

- If `hermes-switchyard` is absent, enable it or inspect the install result.
- If the plugin is enabled but no recommendation appears, check that the current process is fresh and that the request matches an available skill or configured candidate.
- If the local path abstains, inspect the threshold and margin settings. Lowering them increases selection frequency; these are uncalibrated local policies, not quality probabilities.
- If hosted Jev is not attempted, inspect the redacted routing reason. `ack_required` means standing acknowledgement is false; `local_scan_*` means the plugin rejected the bounded task; `per_turn_policy_unknown`, `per_turn_policy_invalid`, `per_turn_policy_denied`, and `restricted_data_class` mean the optional host envelope failed closed. `client_unavailable` means no configured route was available. To replace the profile-scoped credential without exposing it, use Switchyard's masked provider setup:

```text
hermes jev-decision setup --provider typesafe
# or: hermes jev-decision setup --provider openrouter
```

- If hosted Jev is unavailable, local matching remains the only safe result. The plugin does not silently switch models, providers, accounts, or fallback routes.

Do not treat a recommendation as proof that a skill was loaded or followed. If the skill is needed, invoke it through Hermes' normal skill workflow and verify the resulting work independently.
