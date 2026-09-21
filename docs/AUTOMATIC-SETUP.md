# Set up automatic skill recommendations

This guide enables the implemented automatic skill recommendation hook for the `hermes-switchyard` plugin.

Install defaults are `hosted_sanitized` routing, `load` consumer mode, and standing acknowledgement `true`. After install and saving one Jev key, ordinary turns may construct hosted Jev when the local restricted-pattern scan is clean (or when the host forwards an allow envelope). Opt down to `local_only` / `advisory` for privacy. Advisory mode cannot host (`consumer_contract_unmet`). This guide does not claim that a recommendation certifies model quality or GUI completion.

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

## 2. Save one Jev key (happy path)

```text
hermes switchyard setup --provider typesafe
# or: hermes switchyard setup --provider openrouter
```

Setup also runs `ensure-toolsets` so `computer_use` and `hermes_switchyard` are selectable. Start a fresh Hermes process after installation or configuration changes. A new turn in an old process is not sufficient proof that the new hook settings loaded.

Confirm the plugin remains enabled:

```text
hermes plugins list --enabled --plain
```

No `hermes config set` commands are required to turn automatic hosted routing and load mode on; those are the install defaults.

## 3. Run a public smoke

With a key saved:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

Expected behavior when `docker-management` is available to the session:

- load mode may pass one accepted exact identifier to Hermes' normal `skill_view` loader;
- typed callback metadata / local receipt report consumer status and whether the load occurred;
- the system prompt and active Hermes model are unchanged;
- hosted Jev may run when acknowledgement is true and either a host allow envelope is present or the local scan is clean (`egress_authority: standing_ack`).

If the profile registry is empty, the candidate list is ambiguous, or the request has no overlap, abstention is expected.

For a controlled candidate list, set YAML/JSON as one shell argument:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The plugin validates candidate names and descriptions. This setting does not load or authorize the listed skills; it only supplies local ranking data.

## 4. Opt down for privacy

Keep all automatic hosted construction off:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
```

`local_only` never constructs a Jev client. The local matcher uses Hermes' active profile `skills_list()` registry. It searches the full registry locally and abstains when the score is too low or the winner is too close to the runner-up.

Deliver advisory context without auto-loading:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory
```

Refuse hosted automatic routing (tool-call acknowledgement is separate):

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack false
```

Start a fresh Hermes process after changing these settings.

## 5. Host envelopes and standing acknowledgement

Hosted Jev requires either a TypeSafe account/key or an OpenRouter account/key, plus available allowance. `jev_provider: auto` prefers direct TypeSafe. Codex or ChatGPT subscription billing does not pay for either route.

With the install defaults (`hosted_sanitized` + `load` + acknowledgement true):

- If the host forwards an allow envelope, that envelope authorizes the turn (`egress_authority: host_envelope`). Example:

```json
{"version":1,"decision":"allow","data_class":"sanitized","reason_code":"host_policy_allowed","allowed_payload":"sanitized public task"}
```

- If no envelope is present and the local restricted-pattern scan is clean, standing acknowledgement authorizes the turn (`egress_authority: standing_ack`) using the bounded task text that passed the scan.
- Explicit deny, unknown, malformed, or restricted envelopes fail closed before client construction.
- Restricted local-scan hits fail closed before client construction.
- Advisory consumer mode records `consumer_contract_unmet` and never constructs a client.

`always` calls Jev even for a confident local match when load mode and authorization are present. Use `uncertain_only` only as an explicit latency-saving override. A valid hosted abstention stays abstained; only an unavailable transport may preserve a local recommendation.

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
hermes config unset plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode
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

`hermes switchyard status` reports local readiness, tool exposure, provider, and whether a fresh session is required. It never prints the task, candidate descriptions, history, or credential value.

- If `hermes-switchyard` is absent, enable it or inspect the install result.
- If the plugin is enabled but no recommendation appears, check that the current process is fresh and that the request matches an available skill or configured candidate.
- If the local path abstains, inspect the threshold and margin settings. Lowering them increases selection frequency; these are uncalibrated local policies, not quality probabilities.
- If hosted Jev is not attempted, inspect the redacted routing receipt `hosted_skip_reason`. `consumer_contract_unmet` means consumer mode is `advisory`; `ack_required` means standing acknowledgement is false; other `local_scan_*` codes mean the plugin rejected the bounded task; `per_turn_policy_unknown`, `per_turn_policy_invalid`, `per_turn_policy_denied`, and `restricted_data_class` mean the host envelope failed closed. `client_unavailable` / credential-required status means no configured route was available. To replace the profile-scoped credential without exposing it, use Switchyard's masked provider setup:

```text
hermes switchyard setup --provider typesafe
# or: hermes switchyard setup --provider openrouter
```

- If tools are missing from the session catalog, re-run `hermes switchyard ensure-toolsets`.
- If hosted Jev is unavailable, local matching remains the only safe result. The plugin does not silently switch models, providers, accounts, or fallback routes.

Do not treat a recommendation as proof that a skill was followed. In advisory mode it was not loaded. In load mode, verify the typed `skill_recommendation` metadata or local receipt (`consumer_status`, `loaded_skill`, and `skill_load_verified`) before claiming that Hermes' normal loader accepted it; independently verify the resulting work in either mode.
