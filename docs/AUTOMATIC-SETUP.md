# Automatic skill routing: setup

**What it does:** at the start of each turn, Switchyard looks at the skills in your active Hermes profile, works out which one (if any) fits your request, and loads it through Hermes' normal skill loader. You don't have to remember skill names or say "use the docker skill."

**How it decides:** by default it asks Jev, after secret scrubbing. Unless Hermes has already authorized the turn, a local privacy scan runs first. You can switch it to on-device word matching only.

**It's on by default.** Once you've installed the plugin and saved a key, there's nothing else to turn on. This page shows how to try it, tune it, make it more private, or turn it off. For the full internals, see [how automatic routing works](AUTOMATIC-INTEGRATION.md).

> A recommendation is not proof of quality. It means a skill was chosen and, in `load` mode, loaded. Whether the final answer is good is still up to you to judge.

## The defaults in one table

| Setting | Default | Meaning |
| --- | --- | --- |
| `automatic_skill_routing_mode` | `hosted_sanitized` | May ask Jev when the local scan is clean, or when Hermes authorizes the turn |
| `automatic_skill_consumer_mode` | `load` | Loads the chosen skill (instead of just suggesting it) |
| `automatic_skill_public_or_sanitized_data_ack` | `true` | Your standing agreement that turns sent to Jev are public or sanitized |

## 1. Install (optionally pinned to an exact commit)

For a reproducible install, pin the exact 40-character commit you reviewed:

```text
hermes plugins install bgrablin/hermes-switchyard --ref FULL_40_SHA --enable
hermes plugins list --enabled --plain
```

Replace `FULL_40_SHA` with that commit. Never put a credential in the repository URL or your shell history. To install and enable as separate steps, use `--no-enable`, then:

```text
hermes plugins enable hermes-switchyard
```

To check a local checkout without touching your live profile:

```text
hermes plugins doctor . --ci
```

Plugin Doctor checks that the plugin is discovered, imports cleanly, and registers its hooks and tools. It doesn't test model quality, privacy, skill correctness, or GUI behavior.

## 2. Save one Jev key

```text
hermes switchyard setup --provider typesafe
# or: hermes switchyard setup --provider openrouter
```

Setup also runs `ensure-toolsets`, so the `computer_use` and `hermes_switchyard` toolsets are available.

**Start a fresh Hermes process** after installing or changing settings. Sending a new message in an old process isn't enough, because it keeps the old settings.

## 3. Try it

Send a request that clearly matches a skill you have:

```text
hermes chat -q "Diagnose an exiting Docker Compose container"
```

If a `docker-management` skill is available in that session, you should see:

- that one skill loaded through Hermes' normal `skill_view` loader;
- a receipt (`hermes switchyard receipt --json`) showing whether the load happened;
- your system prompt and active model unchanged.

Jev is consulted when the acknowledgement is `true` and the local scan is clean (receipt field `egress_authority: standing_ack`), or when Hermes forwards an "allow" policy for the turn.

**Getting "no skill"?** That's expected when your profile has no skills, when two skills are equally good, or when nothing matches.

### Make the test predictable

To test against a fixed candidate list instead of your whole catalog, pass the list as one quoted argument:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_candidates '[{"name":"docker-management","description":"Manage Docker containers and Compose services."}]'
```

Switchyard validates the names and descriptions. This list only feeds the ranking; it doesn't load or authorize those skills.

## 4. Make it more private

**Keep routing on your machine.** `local_only` never creates a Jev client:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
```

The local matcher reads your profile's full skill list (`skills_list()`) and picks the best word match. It stays silent if the best score is too low or too close to the runner-up.

**Suggest skills without loading them:**

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory
```

Advisory mode never calls Jev. Receipts show `consumer_contract_unmet` for the skipped hosted step.

**Refuse hosted routing.** This doesn't affect explicit tool calls, which have their own acknowledgement:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack false
```

Start a fresh Hermes process after any of these.

## 5. When is a turn allowed to reach Jev?

Jev needs a TypeSafe or OpenRouter key with available credit. `jev_provider: auto` prefers TypeSafe. Codex or ChatGPT billing doesn't cover either.

With the defaults (`hosted_sanitized` + `load` + acknowledgement `true`):

| Situation | Result |
| --- | --- |
| Hermes forwards an **allow** policy for the turn | Allowed (`egress_authority: host_envelope`). The policy's payload is scrubbed but not re-scanned locally |
| No policy from Hermes, and the local scan is clean | Allowed, using the scanned and scrubbed text (`egress_authority: standing_ack`) |
| Hermes forwards a **deny**, unknown, malformed, or restricted policy | Blocked before any Jev client is created |
| The local scan finds restricted content | Blocked before any Jev client is created |
| Consumer mode is `advisory` | Never calls Jev (`consumer_contract_unmet`) |

An example allow policy, for hosts that send one:

```json
{"version":1,"decision":"allow","data_class":"sanitized","reason_code":"host_policy_allowed","allowed_payload":"sanitized public task"}
```

**`always` vs `uncertain_only`:** `always` (the default) asks Jev even when local matching is confident. `uncertain_only` saves latency by asking only when local matching is unsure. If Jev validly says "no skill," that answer stands. Switchyard falls back to a local pick only when Jev couldn't be reached at all.

## 6. Turn it off or roll back

**Turn off automatic recommendations** but keep the rest of the plugin:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_recommendation false
```

**Stop hosted requests** but keep local matching:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
```

**Reset everything to defaults:**

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

**Disable the whole plugin:**

```text
hermes plugins disable hermes-switchyard
```

**Go back to an earlier reviewed version** by reinstalling that exact commit:

```text
hermes plugins remove hermes-switchyard
hermes plugins install bgrablin/hermes-switchyard --ref PREVIOUS_FULL_SHA --enable
```

Keep a note of the previous 40-character SHA as your rollback target. Confirm the installed revision with `hermes plugins list --plain`, and run Plugin Doctor before using it. Start a fresh process after any of these changes.

## 7. Troubleshooting

Start with:

```text
hermes plugins list --enabled --plain
hermes switchyard status
```

`status` reports readiness, tool exposure, the provider, and whether you need a fresh session. It never prints your task, skill descriptions, history, or key.

| Symptom | Likely cause and fix |
| --- | --- |
| `hermes-switchyard` isn't listed | It isn't enabled. Enable it, or check the install output. |
| Enabled, but no recommendation | The process isn't fresh, or no skill matches the request. Restart, and check your skills or candidate list. |
| Local matching always abstains | Try lowering `automatic_skill_local_threshold` or `automatic_skill_local_margin`. These are policy knobs, not quality probabilities. |
| Jev never gets called | Read `hosted_skip_reason` in the receipt (see below). |
| Tools missing from the session | Run `hermes switchyard ensure-toolsets`. |

What `hosted_skip_reason` means:

| Reason | Meaning |
| --- | --- |
| `consumer_contract_unmet` | Consumer mode is `advisory`. |
| `ack_required` | The standing acknowledgement is `false`. |
| `local_scan_*` | The local scan kept the turn local. |
| `per_turn_policy_unknown`, `per_turn_policy_invalid`, `per_turn_policy_denied`, `restricted_data_class` | Hermes' per-turn policy blocked hosting. |
| `client_unavailable`, or status `credential_required` | No usable key or route. Re-run the masked setup: |

```text
hermes switchyard setup --provider typesafe
# or: hermes switchyard setup --provider openrouter
```

If Jev is unavailable, local matching is the only result. Switchyard never silently switches models, providers, accounts, or fallback routes.

**Was the skill actually used?** In `advisory` mode, nothing was loaded. In `load` mode, check the receipt's `consumer_status`, `loaded_skill`, and `skill_load_verified` fields before assuming Hermes accepted the skill. Either way, check the resulting work yourself.
