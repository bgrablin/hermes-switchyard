# Hermes Switchyard

Jev-powered advisory selection, general typed assessment, and cross-platform Cua Driver computer-use support for Hermes Agent.

Version: 0.4.2

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is a structured decision model. Hermes Switchyard is the Hermes plugin integration around Jev: it applies plugin-owned local policy, supports standalone hosted routing with standing consent and per-turn scanning, keeps actions bounded, and leaves final verification to Hermes. A future Hermes turn envelope may strengthen a decision with a narrower sanitized payload, but is optional. The acknowledgement is not Hermes-owned DLP or automatic authorization. Jev owns typed judgments; Switchyard owns validation, routing, execution boundaries, and evidence.

Jev supplies decision scores. Switchyard applies its eligibility and confidence rules; for model routing, it selects the cheapest qualified model. Hermes checks the result. This plugin uses Jev; it does not provide every feature that Jev supports.

![Hermes Switchyard flow from Jev decisions through Switchyard validation to Hermes execution and verification](docs/assets/hermes-switchyard-overview.png)

Switchyard gives Hermes another way to choose among a defined set of options. It does not modify Hermes core, silently change the active model, or claim that a recommendation or GUI action is correct. Automatic skill loading is opt-in; the default remains advisory.

## Install

### Quick install

Install from the public GitHub repository. No GitHub login or token is required:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

The command installs and enables the plugin. To inspect the installed files before enabling it:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins list
hermes plugins enable hermes-switchyard
```

The repository command requires no GitHub login or token. Catalog installation is not available until a human admits the plugin to the Hermes catalog; use the repository command above.

After installing or updating, start a fresh Hermes session so it loads the new plugin. Restart only the Hermes process that needs to load the change.

## Setup requirements

A working Jev call requires one of these profile-scoped secrets:

- `TYPESAFE_API_KEY` for the direct TypeSafe endpoint. This is the preferred low-latency route when available.
- `OPENROUTER_API_KEY` for OpenRouter's Decisions endpoint.
- Enough account credit or allowance for the selected route.

`jev_provider: auto` prefers direct TypeSafe when `TYPESAFE_API_KEY` exists and otherwise uses OpenRouter. Set `jev_provider` to `typesafe` or `openrouter` to pin the route. A ChatGPT or Codex subscription is separate from both accounts and does not pay Jev request charges.

The supported endpoints are `https://api.typesafe.ai/v1/systemone` and `https://openrouter.ai/api/alpha/decisions`. Arbitrary endpoints, redirects, and provider fallbacks are rejected. The client keeps a connection alive across decisions so a multi-step CUA loop does not pay a new TLS setup on every step.

Use the secure setup steps in [docs/SETUP.md](docs/SETUP.md). Never pass an API key with a command-line argument or store it in a URL, repository file, fixture, or issue report.

## Supported features

- **General assessment:** `jev_assess` exposes Choice, Score, and Noul through validated bounded requests. Large independent question sets are batched without dropping questions; the plugin never turns a probability into an unreviewed side effect.
- **Skill selection:** `jev_skill_select` recommends one skill from the candidate list supplied by Hermes. Catalogs larger than Jev's per-Choice limit are searched with partition fan-out and recursive reduction; no tail is silently discarded. It never loads the skill.
- **Multi-skill selection:** `jev_skill_select_many` independently scores the complete bounded catalog and returns a typed list of exact skill identifiers. It is a separate advisory contract and never loads or mutates skills.
- **Model routing:** `jev_model_route` is the documented Hermes routing point. It filters candidates using explicit code-owned metadata and requirements, then recommends the lowest-cost qualified candidate. `route_model_from_registry` supplies a real approved candidate registry at that point. It never changes the active Hermes model and does not try another provider when Jev fails. Stale registry generations abstain as `stale_registry`; an empty registry abstains as `empty_registry`.
- **Cua Driver computer use:** `jev_computer_use` is registered in the `computer_use` toolset so it is callable in Windows sessions that enable native computer use without also naming the plugin toolset. It delegates to Hermes' existing Cua Driver-backed `computer_use` tool on Windows, macOS, and Linux. Jev first chooses an operation, then only the relevant bounded target family; dense-partition finalists receive a global Choice. Fresh capture identity checks remain mandatory.

## Automatic skill recommendations

When the plugin is enabled, the `pre_llm_call` lifecycle hook is on by default. It discovers the **full** active profile skill registry through Hermes' supported `skills_list` API and performs a fast local match. In `hosted_sanitized` mode, standing public-or-sanitized acknowledgement and a strict plugin-owned local per-turn scan are required; a compatible host may optionally provide an allowed envelope with a narrower sanitized payload. Set `automatic_skill_routing_mode` to `local_only` or `off` when hosted routing is not wanted. Set `automatic_skill_jev_mode` to `uncertain_only` only when latency matters more than Jev coverage.

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode hosted_sanitized
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_jev_mode always
```

The local scan rejects restricted data before the hosted client is constructed. A host envelope, when supplied, must contain `version: 1`, `decision: "allow"`, `data_class: "public"` or `"sanitized"`, and bounded `allowed_payload`; explicit denied, unknown, restricted, or malformed envelopes fail closed. Without an envelope, the accepted bounded task is the hosted payload when standing acknowledgement is true and the local scan allows it. False acknowledgement always blocks hosted construction.

Automatic hosted Jev receives only the accepted bounded task and exact candidate identifiers. Candidate descriptions, conversation history, and full skill bodies remain local. A valid Jev abstention is preserved; a transport failure may preserve a local winner. The hook exposes only redacted routing status/reason metadata.

The default `automatic_skill_consumer_mode: advisory` adds model-visible context without loading anything. Set it to `load` to pass one accepted exact identifier to Hermes' normal `skill_view` loader once per turn. Explicit skill instructions, abstention, invalid results, conflicts with configured mandatory skills, and loader errors do not trigger an automatic load. Typed callback metadata and the local receipt report the selected identifier, source, consumer status, and whether the load occurred.

## Privacy and data handling

Jev tools require `public_or_sanitized_data_ack: true` in their input. This means the caller gives standing consent for the plugin-owned bounded classification path. The flag does not replace local scanning, grant unrestricted permission to share data, or bypass other controls. Automatic skill recommendations require the persistent setting plus a strict local per-turn scan; a future Hermes envelope is optional strengthening.

For Cua Driver computer use, Jev may receive the goal, target application, window title, safe control labels, visible context, and recent actions through the selected Jev endpoint. Text-field operations use only bounded caller-supplied values from `text_inputs`; the registered tool never calls a conversational Hermes LLM between Jev actions and abstains when no caller value is supplied. Do not send private, employer, regulated, credential, password, API-key, token, payment, or verification-code data.

## Evidence, reconciliation, and deadlines

`jev_computer_use` returns one typed receipt that records what is known, not a single success boolean. A dispatched action records the native `verdict`, whether the executor `effect_confirmed` the change, the `effect_status` string, and any `escalation`. These are distinct evidence levels: a native verdict is not proof a downstream task finished, load-mode verification is not proof a recommendation was correct, and an observed postcondition is not proof the whole goal was satisfied. Every receipt keeps `verified: false` with `verification_owner: coordinator` until Hermes independently checks the postcondition.

Expected exceptions preserve partial progress instead of discarding it. If a later action, fresh capture, or native dispatch fails, the receipt still lists every prior action, decision, provider request, and cost, and it sets `reconcile_before_retry: true` when any side effect may already exist. That flag asks the coordinator to inspect before replaying; it is not a claim that replaying is safe.

Provider usage after a partial failure can be incomplete. A missing usage value is not zero usage. A receipt may report a partial subtotal from completed responses and mark the operation cost incomplete rather than claiming a finished total.

Operation deadlines are cooperative, not hard. The loop checks `operation_remaining_deadline()` before each Jev request and native action and bounds each request by the time left. Native operations such as a blocking dispatcher call or a lock acquisition may not be interruptible mid-flight, so the plugin does not claim to force a desktop action to stop instantly and never retries silently after the caller believes the operation stopped.

## Tools and limits

The tools are advisory and bounded:

- A high confidence score is not proof that a choice is correct.
- Switchyard can return no selection when eligibility or confidence checks fail. This valid result is called abstention.
- The default advisory consumer does not load skills. The opt-in `load` consumer invokes Hermes' normal loader once for an accepted turn; neither mode changes runtime models or certifies GUI completion.
- Provider fallback is disabled. A failed Jev request does not silently move to another provider.
- Each assessment, skill-selection, or model-routing operation has one aggregate 64-request budget. A CUA run has one aggregate 256-request budget across its 100-action ceiling; serialized request size is also bounded.
- Skill selection and model routing work wherever Hermes can expose the plugin toolset. `jev_computer_use` is available on Windows, macOS, and Linux when Hermes' Cua Driver-backed `computer_use` tool is available.
- The repository's offline tests use synthetic transports and do not call OpenRouter or drive a real GUI.

Future work includes a reviewed catalog admission, independent real-GUI coverage, and comparative evaluation. Those are not provided by this release.

## Safe credential setup

The plugin can use either `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY`. Both are optional alternatives, so plugin installation does not prompt for either one. After installation, save one key through Switchyard's masked setup command. With `jev_provider: auto`, direct TypeSafe is preferred when both are present.

```text
hermes plugins install bgrablin/hermes-switchyard --enable
hermes switchyard setup --provider typesafe
hermes config set plugins.entries.hermes-switchyard.settings.jev_provider auto
```

Do not use `hermes auth add openrouter` for this plugin. Switchyard reads profile-scoped secrets through Hermes' secret scope. Check readiness without displaying a key:

```text
hermes plugins list --enabled
hermes plugins doctor /path/to/hermes-switchyard --ci
```

## Cua Driver prerequisites

`jev_computer_use` reuses Hermes' existing Cua Driver-backed `computer_use` tool. Install and diagnose that toolset through Hermes, not by vendoring a second driver into Switchyard:

```text
hermes computer-use install
hermes computer-use doctor
hermes -t computer_use chat
```

Cua Driver supports background desktop actions on Windows, macOS, and Linux. Switchyard adds the Jev decision layer, application-owned candidate IDs, partitioned target choices, fresh identity checks, and independent-completion semantics. It does not bypass Hermes approval or Cua Driver safety controls.

## Configuration

Settings are profile-scoped under `plugins.entries.hermes-switchyard.settings`:

```text
hermes config set plugins.entries.hermes-switchyard.settings.jev_provider auto
hermes config set plugins.entries.hermes-switchyard.settings.computer_max_steps 100
```

`jev_provider` is `auto`, `typesafe`, or `openrouter`. `api_endpoint` may only be the fixed direct TypeSafe or OpenRouter endpoint. Leave `jev_model` empty to select the provider default. Each Hermes profile has its own settings and secret scope.

## Missing-key symptoms

When neither `TYPESAFE_API_KEY` nor `OPENROUTER_API_KEY` is available, Hermes can disable the plugin during loading. If a handler is reached without a key, the plugin fails closed with a generic request-validation error; it does not print credentials or provider response text.

The supported recovery is:

1. Run `hermes switchyard setup --provider typesafe` or use `--provider openrouter` and enter the key only in the masked prompt.
2. Start a fresh Hermes session.
3. Run `hermes plugins list --enabled`.
4. From the plugin root, run the native check:

```text
hermes plugins doctor . --ci
```

Plugin Doctor checks whether Hermes can import and register the plugin. It does not test a live Jev request or prove that a GUI task succeeded. It runs plugin code in-process, not in a sandbox, so use it only with trusted code.

## Updating and rollback

For an unpinned repository install:

```text
hermes plugins update hermes-switchyard
```

An exact-SHA install does not move implicitly. Remove the installed copy, reinstall the reviewed commit with `--ref` as described in [docs/RELEASE.md](docs/RELEASE.md), then enable the plugin if required. Check the result with `hermes plugins list` and `hermes plugins doctor . --ci` before enabling it.

These operations replace only the plugin under the active profile's plugin directory. They do not patch Hermes core. Keep the previous reviewed SHA as the rollback target.

## Offline verification

The repository has no runtime Python dependency beyond Hermes for native loading and requires Python 3.11 or newer for offline checks:

```text
python -m unittest discover -s tests -v
python evaluation/evaluate.py --validate
python scripts/check_portability.py
```

Release archives use an exact Git source commit, include `SOURCE-MANIFEST.json` and embedded `SHA256SUMS`, and are extracted and verified before the builder returns. Release and candidate review instructions are in [docs/RELEASE.md](docs/RELEASE.md).

## Documentation

- [Setup](docs/SETUP.md)
- [Release instructions](docs/RELEASE.md)
- [Feature and test matrix](docs/TEST-MATRIX.md)
- [Contributing](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)
- [Third-party references](THIRD_PARTY.md)
- [Changelog](CHANGELOG.md)
- [Brand assets](docs/assets/hermes-switchyard-branding.png)

Own work is MIT-licensed. See `THIRD_PARTY.md` for conceptual upstream references.