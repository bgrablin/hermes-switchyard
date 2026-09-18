# Hermes Switchyard

Jev-powered selection for Hermes Agent.

A native standalone Hermes plugin for bounded, advisory Jev decisions. It adds closed-set skill selection, explicit-metadata model routing, and a Windows computer-use loop without modifying Hermes core.

Version: 0.3.2

## Tools

- `jev_skill_select` chooses from the caller's explicit skill candidates, or abstains when the local confidence, need, or winning-probability gates do not pass. It never loads a skill or edits the prompt.
- `jev_model_route` filters candidates locally by explicit approval, data classes, capabilities, context limit, and cost. Jev supplies fit signals; code selects the cheapest qualified candidate. It never changes the runtime model and never tries an automatic fallback.
- `jev_computer_use` is a Windows-only multi-step pilot over Hermes' existing `computer_use` tool. It requires a complete goal, target application, explicit hotkey allowlists, and the public/sanitized-data attestation. `DONE` returns a completion candidate with `verified: false`; independent verification remains the coordinator's job.

All three tools preserve the existing Hermes approval and dispatch surface. The model endpoint, model aliases, and provider fallback policy are closed sets in code. The plugin does not provide a quality-calibration claim, guardrail management, arbitrary endpoint selection, or credential-bearing destinations.

## Automatic skill recommendations

When the plugin is enabled, the `pre_llm_call` lifecycle hook is on by default.
It discovers the active profile's skills through Hermes' supported `skills_list`
registry, performs bounded local matching, and may add one advisory recommendation
to the current user API message. This is turn-scoped context: it does not mutate
the cached system prompt, load a skill, or change the toolset. Disable it with:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_recommendation false
```

Hosted Jev selection is off by default. Enable it only when the active profile is
permitted to send public or already-sanitized task text and exact candidate names:

```text
hermes config set plugins.entries.jev-decision.settings.automatic_skill_jev true
hermes config set plugins.entries.jev-decision.settings.automatic_skill_public_or_sanitized_data_ack true
```

The persistent acknowledgement covers the bounded current task and exact skill
identifiers only. Candidate descriptions and conversation history remain local;
the acknowledgement is an operator attestation, not DLP or authorization. A
valid hosted abstention is preserved; local fallback is used only when the
hosted decision is unavailable.

## Data boundary

Manual model-facing tools require `public_or_sanitized_data_ack: true` and all
model-facing operations must use public or already-sanitized data. This is a
caller/operator attestation, not DLP or authorization. Do not send private,
employer, regulated, credential, payment, or verification UI/data; regex
filtering is not permission to send data.

## Install from the private repository

Use a Hermes version with native `plugin.yaml` plugins and the current plugin CLI. Authenticate to GitHub before a private clone with `gh auth login` or an already-configured Git credential helper. Do not put a token in a clone URL or shell history.

The repository is a root-layout native plugin. Install from the repository root; there is no subdirectory install flag:

```text
hermes plugins install bgrablin/hermes-switchyard --ref FULL_40_SHA --no-enable
hermes plugins list
hermes plugins enable jev-decision
```

Open a fresh Hermes session after installing or updating. An existing gateway needs a controlled restart only when you want it to load the new plugin; do not restart unrelated services.

`FULL_40_SHA` must be the exact 40-character commit you choose to install. Hermes records the pinned source and revision. `--no-enable` leaves the plugin installed but inactive until the explicit enable step.

The installer may request `OPENROUTER_API_KEY` through a masked Hermes prompt because the manifest declares it as a secret. Configure it through Hermes' native secret flow for the active profile. Never paste a key into this README, a command line, a Git URL, a tool argument, or a test fixture.

## Per-profile configuration

Plugin settings are profile-scoped under `plugins.entries.jev-decision.settings`. Use Hermes configuration commands for the active profile, for example:

```text
hermes config set plugins.entries.jev-decision.settings.jev_model typesafe/jev-1.13
hermes config set plugins.entries.jev-decision.settings.computer_max_steps 12
```

The endpoint remains fixed by the implementation even if a setting attempts to change it. Each profile needs its own secret/configuration; profiles do not inherit credentials from one another. Use `hermes config get` for masked readback and the profile's native secret/configuration management, not shell credential values.

## Updating and rollback

For an unpinned install, the normal update command is:

```text
hermes plugins update jev-decision
```

A pinned install does not move implicitly. To update or roll back, reinstall the same private repository with `--force` and the desired full commit SHA, then enable the plugin if required:

```text
hermes plugins install bgrablin/hermes-switchyard --force --ref FULL_40_SHA --no-enable
hermes plugins enable jev-decision
```

These operations replace only the plugin under the profile's plugin directory. They do not patch Hermes core. Keep the previous full SHA as the rollback target and verify with `hermes plugins list` and `hermes plugins doctor` before enabling it.

## Offline verification

The repository has no runtime Python dependency beyond Hermes for native loading and no dependency beyond Python 3.11+ for its offline tests:

```text
python -m unittest discover -s tests -v
python evaluation/evaluate.py --validate
```

The evaluator uses only public synthetic fixtures by default. It writes `evaluation/results.json`, which is ignored by Git, and records repository-relative source information rather than host-specific absolute paths. `--live` is explicit, bounded, and requires the caller to provide the API key through the native environment/secret scope; CI never uses it.

To validate native discovery without changing a live profile, point `HERMES_HOME` at a fresh temporary directory and run Plugin Doctor from the repository root:

```text
HERMES_HOME=<fresh-temporary-directory> hermes plugins doctor . --ci
```

Doctor uses isolated registration and a temporary Hermes home. It is a validation command, not an install or enable operation. A plugin still runs in-process when enabled, so only validate code you trust.

## Supported scope and limitations

- Native general-plugin discovery is supported on Hermes hosts that implement `plugin.yaml` plus root `__init__.py` with `register(ctx)`.
- Linux and Windows Python 3.11+ are covered by offline CI. The computer-use pilot remains Windows-only; routing and skill selection are host-independent.
- Synthetic tests do not call the network or drive a real GUI. A live smoke is not a substitute for coordinator-owned verification.
- Jev outputs are advisory and uncalibrated. Abstention is a valid result. The plugin does not mutate the cached system prompt, load skills automatically, change runtime models, or certify GUI completion; automatic recommendations are turn-scoped context only.

## License and references

Own work is MIT-licensed. See `THIRD_PARTY.md` for conceptual upstream references and `SECURITY.md` for reporting boundaries.
