# Setup

Hermes Switchyard has two separate setup boundaries:

1. GitHub access to download the private repository.
2. OpenRouter access for live Jev decisions.

A GitHub login does not provide the OpenRouter key, credit, or model access needed by Jev. A ChatGPT or Codex subscription is also separate and does not pay Jev or OpenRouter request charges.

## Requirements

For skill selection and model routing:

- Hermes Agent with the native plugin contract: `plugin.yaml`, a root `__init__.py`, and `register(ctx)`.
- Python 3.11 or newer for the repository's offline checks.
- GitHub read access to `bgrablin/hermes-switchyard` while the repository is private.
- An OpenRouter account.
- An OpenRouter API key in the active Hermes profile under the exact name `OPENROUTER_API_KEY`.
- Enough OpenRouter credit or current account allowance for the Jev request.
- OpenRouter access to `typesafe/jev-1.13`.

For `jev_computer_use`, Hermes must run on Windows and the target application must be installed and available through Hermes' normal computer-use path. Skill selection and model routing are host-independent.

Direct TypeSafe account access, a TypeSafe API key, and a direct TypeSafe endpoint are not supported. The plugin uses only `https://openrouter.ai/api/alpha/decisions` and the model aliases enforced by its code.

## Install

If Git does not already have access to the private repository, authenticate GitHub with `gh auth login` or configure a Git credential helper. Do not put a token in a Git URL.

Install and enable the plugin with the supported one-liner:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

Hermes can prompt for the manifest's required `OPENROUTER_API_KEY` during installation. Enter it only in Hermes' masked prompt. To install without enabling first:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins list
hermes plugins enable jev-decision
```

Start a fresh Hermes session after installation or an update.

## Add the OpenRouter key safely

If the plugin is already installed, use Hermes' secure provider prompt:

```text
hermes auth add openrouter --type api-key
```

Leave out `--api-key` so the value is requested interactively rather than placed in shell history. Never put the key in `hermes config set`, a shell variable saved to a file, a URL, a fixture, a repository file, or an issue report.

Check provider status without displaying the key:

```text
hermes auth status openrouter
```

The plugin reads the active profile's `OPENROUTER_API_KEY` secret. Profiles do not share this secret automatically. After adding or changing it, start a fresh Hermes session.

## Confirm the plugin

List enabled plugins:

```text
hermes plugins list --enabled
```

Run the native plugin check from the plugin root with a fresh temporary Hermes home when you do not want to change a live profile:

```text
HERMES_HOME="$(mktemp -d)" hermes plugins doctor . --ci
```

On Windows, set `HERMES_HOME` to a new temporary directory using the shell's normal environment-variable syntax. Plugin Doctor imports and registers plugin code in-process, so it checks the real loader but is not a sandbox. Use it only with reviewed code.

A successful native check proves discovery and registration, not model quality, GUI completion, or permission to send private data.

## Configure the plugin

The plugin settings are profile-scoped under `plugins.entries.jev-decision.settings`:

```text
hermes config set plugins.entries.jev-decision.settings.jev_model typesafe/jev-1.13
hermes config set plugins.entries.jev-decision.settings.computer_max_steps 12
```

The default Jev model is `typesafe/jev-1.13`. The dated alias `typesafe/jev-1.13-20260917` is accepted by the implementation for compatibility. Do not invent a model slug or replace the fixed OpenRouter Decisions endpoint.

Some Windows text-entry actions use the host-owned Hermes text model. If Hermes has no configured model, choose one through the normal interactive command:

```text
hermes model
```

That model is separate from Jev. A Codex login can supply Hermes' host model when configured, but it does not supply the OpenRouter account, key, credit, or Jev access.

## Privacy requirements

Before a Jev tool runs, the invoking code must set `public_or_sanitized_data_ack: true`. This confirms that the data was reviewed before it is sent. It is not a scan, a redaction guarantee, data-loss-prevention control, authorization to share, or permission to bypass another control.

For a Windows computer-use request, the decision state can include the goal, target application, window title, safe visible controls, visible context, and recent actions. Text entry can also send the goal, selected field, visible context, and recent actions to the configured Hermes text model. Do not send private, employer, regulated, credential, password, API-key, token, payment, or verification-code data.

## What is supported

- `jev_skill_select` recommends a skill from the candidate list supplied by Hermes. It does not load the skill.
- `jev_model_route` filters and ranks the candidate metadata supplied by Hermes. It does not change the active model or use a fallback provider.
- `jev_computer_use` runs bounded actions in a specified Windows application through Hermes' normal approval and action controls. It rechecks the target before acting and returns `verified: false` until Hermes independently checks the result.
- Jev may abstain. A confidence value is not a correctness guarantee.

## Missing-key symptoms and recovery

If `OPENROUTER_API_KEY` is missing, Hermes can disable the plugin during loading and the Jev tools will not appear as available. If a request reaches the plugin without a key, it fails closed with a generic request-validation error rather than exposing credential details.

Recover without changing code:

1. Add the key with the secure Hermes provider prompt.
2. Start a fresh Hermes session.
3. Run `hermes plugins list --enabled`.
4. Run `hermes plugins doctor . --ci` from the plugin root if discovery remains unclear.

A Codex login, a different `jev_model` value, or a direct TypeSafe key does not fix a missing OpenRouter key.

## Limits and future work

This release does not provide direct TypeSafe access, automatic skill loading, automatic model changes, provider fallback, calibrated correctness claims, independent GUI completion proof, or a real-GUI benchmark. A reviewed catalog admission and broader real-GUI coverage require separate work and approval.
