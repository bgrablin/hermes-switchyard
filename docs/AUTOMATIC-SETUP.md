# Set up automatic skill recommendations

This guide enables the implemented automatic skill recommendation hook for the `jev-decision` plugin.

The default path is local and does not call Jev. Hosted Jev is a separate, explicitly enabled path. Neither path loads a skill or changes the active Hermes model.

## 1. Install the pinned plugin

Install an exact 40-character commit and enable the plugin:

```text
hermes plugins install bgrablin/hermes-switchyard --ref FULL_40_SHA --enable
hermes plugins list --enabled --plain
```

Replace `FULL_40_SHA` with the commit you intend to run. Do not put a credential in the repository URL or shell history. If you want installation and enablement as separate steps, use `--no-enable` and then:

```text
hermes plugins enable jev-decision
```

Validate a checkout without changing a live profile:

```text
hermes plugins doctor . --ci
```

Plugin Doctor checks discovery, import, registration, declared hooks, and tools. It does not prove model quality, privacy, skill correctness, or GUI completion.

## 2. Start with local recommendations

Local recommendations are enabled by default. To make the setting explicit:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_recommendation true
hermes config set plugins.entries.jev-decision.settings.automatic_skill_jev false
hermes config set plugins.entries.jev-decision.settings.automatic_skill_public_or_sanitized_data_ack false
```

The local path uses Hermes' active profile `skills_list()` registry. It ranks up to 32 candidates and abstains when the score is too low or the winner is too close to the runner-up.

For a controlled candidate list, set YAML/JSON as one shell argument:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

The plugin validates candidate names and descriptions. This setting does not load or authorize the listed skills; it only supplies local ranking data.

## 3. Reload in a fresh Hermes process

The plugin reads these settings while it registers the hook. Exit the current Hermes process and start a new one after installation or configuration changes. A new turn in an old process is not sufficient proof that the new hook settings loaded.

Confirm the plugin remains enabled:

```text
hermes plugins list --enabled --plain
```

## 4. Run a public local smoke

No OpenRouter key is required for this local smoke:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

Expected behavior when `docker-management` is available to the session:

- the current user-message context contains an advisory recommendation for the exact identifier `docker-management`;
- the recommendation says that the plugin did not load the skill;
- the system prompt and active Hermes model are unchanged;
- no OpenRouter/Jev request is made in the default local mode;
- no skill-use event is created just because the recommendation was produced.

The recommendation is context sent to the model, not a separate status banner. The assistant may ignore it. If the profile registry is empty, the candidate list is ambiguous, or the request has no overlap, abstention is expected.

## 5. Configure hosted Jev only for public or sanitized tasks

Hosted Jev requires a separate OpenRouter account, an available OpenRouter credit/account allowance, and access to the configured Jev model alias. Codex or ChatGPT subscription billing does not pay for OpenRouter requests. Direct TypeSafe account access is not a supported setup path.

Add the OpenRouter credential through the masked Hermes flow:

```text
hermes auth add openrouter --type api-key
hermes auth status openrouter
```

Do not use `--api-key` in shell history. Do not paste a key into a config value, tool argument, document, or issue. `hermes auth status openrouter` is a readiness check and must not expose the secret.

Enable hosted recommendations only after deciding that every task sent by this feature is public or already sanitized. Candidate descriptions remain local-ranking metadata and are not sent to hosted Jev; exact candidate identifiers are sent with the bounded current task:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_jev true
hermes config set plugins.entries.jev-decision.settings.automatic_skill_public_or_sanitized_data_ack true
```

The confirmation is a caller attestation, not DLP or authorization. It does not provide a blanket privacy or retention guarantee. Do not enable this path for private, employer, regulated, credential, payment, verification, or otherwise restricted content.

The current user task is bounded before the request; conversation history and candidate descriptions remain local. Start a fresh Hermes process after changing either hosted setting, then repeat the public smoke. Hosted failure or timeout may preserve a valid local recommendation; a valid hosted abstention remains abstention and does not select a fallback model or provider.

## 6. Disable or roll back

Disable automatic recommendations while leaving the rest of the plugin enabled:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_recommendation false
```

Disable hosted requests but keep local matching:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_jev false
hermes config set plugins.entries.jev-decision.settings.automatic_skill_public_or_sanitized_data_ack false
```

Remove the automatic settings and return to manifest defaults:

```text
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_candidates
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_local_threshold
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_local_margin
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_cache_seconds
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_jev
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_public_or_sanitized_data_ack
hermes config unset plugins.entries.jev-decision.settings.automatic_skill_recommendation
```

Start a fresh process after disabling or unsetting values.

To disable the complete plugin:

```text
hermes plugins disable jev-decision
```

To return to a previously reviewed plugin revision, reinstall that exact revision:

```text
hermes plugins install bgrablin/hermes-switchyard --force --ref PREVIOUS_FULL_SHA --enable
```

Keep the previous 40-character SHA as the rollback target. Verify the installed revision with `hermes plugins list --plain` and run Plugin Doctor before using it.

## 7. Troubleshooting

`hermes plugins list --enabled --plain`

- If `jev-decision` is absent, enable it or inspect the install result.
- If the plugin is enabled but no recommendation appears, check that the current process is fresh and that the request matches an available skill or configured candidate.
- If the local path abstains, inspect the threshold and margin settings. Lowering them increases selection frequency; these are uncalibrated local policies, not quality probabilities.
- If hosted Jev is not attempted, confirm `automatic_skill_jev: true` and `automatic_skill_public_or_sanitized_data_ack: true`, then check the account without exposing the key:

```text
hermes auth status openrouter
```

- If hosted Jev is unavailable, local matching remains the only safe result. The plugin does not silently switch models, providers, accounts, or fallback routes.

Do not treat a recommendation as proof that a skill was loaded or followed. If the skill is needed, invoke it through Hermes' normal skill workflow and verify the resulting work independently.
