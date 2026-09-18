# Hermes Switchyard

Jev-powered skill and model recommendations for Hermes Agent, with controlled Windows actions.

Version: 0.3.2

![Hermes Switchyard logo and wordmark](docs/assets/hermes-switchyard-branding.png)

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is a model that makes structured decisions. Switchyard connects Jev to Hermes Agent.

Switchyard helps Hermes choose a skill or model from a supplied list. It also supports controlled actions in Windows applications. Hermes must check each result.

Switchyard does not modify Hermes core. It does not load skills automatically or change the active model. It does not provide every Jev feature.

## What you need

- Hermes Agent.
- An OpenRouter account and API key.
- Enough OpenRouter credit or account allowance for Jev requests.
- Access to `typesafe/jev-1.13` through OpenRouter.
- GitHub read access to this repository, which is currently private.

A ChatGPT or Codex subscription does not pay for OpenRouter requests. It does not supply an OpenRouter account or API key.

Switchyard supports OpenRouter access only. A direct TypeSafe account, API key, or endpoint does not replace OpenRouter access.

## Install

If Git cannot read this repository, authenticate with `gh auth login` or configure a Git credential helper.

Run:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

This command installs and enables Switchyard. If Hermes asks for an API key, enter it in the masked prompt.

To inspect the plugin before you enable it, run these commands in order:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins list
hermes plugins enable jev-decision
```

Do not put a GitHub token in a command, clone URL, repository file, or issue report.

Switchyard is not yet in the Hermes catalog. The repository command is the supported installation method.

After an installation or update, start a fresh Hermes session. Restart only the Hermes process that needs the new plugin.

## Configure the OpenRouter key

The plugin requires `OPENROUTER_API_KEY` in the active Hermes profile.

Run the installation command:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

Hermes reads the manifest's `requires_env` entry. It requests the key through a masked prompt and saves it in the profile's `.env`.

If Switchyard is already installed, add `--force` to repeat the installation and secret prompt.

Do not use `hermes auth add openrouter` to supply this plugin's key. That command manages a separate provider credential pool. Switchyard reads the profile secret through `get_secret("OPENROUTER_API_KEY")`.

Check the enabled plugin without exposing the key:

```text
hermes plugins list --enabled
```

If Hermes reports a missing key:

1. Run `hermes plugins install bgrablin/hermes-switchyard --force --enable`.
2. Enter the key in the masked prompt.
3. Start a fresh Hermes session.
4. Run `hermes plugins list --enabled`.

A Codex login or a change to `jev_model` does not supply the missing key.

See the [setup guide](docs/SETUP.md) for more information.

## What the tools do

### Skill selection

`jev_skill_select` recommends one skill from the list that Hermes supplies. Hermes decides whether to load that skill.

### Model routing

`jev_model_route` filters the supplied models against the supplied requirements. It then recommends the lowest-cost qualified model.

The tool does not change the active Hermes model. A failed Jev request does not cause a request to another provider.

### Windows actions

`jev_computer_use` performs limited actions in a specified Windows application. It uses the normal Hermes approval and action controls.

The tool captures the target again before each action. It refuses an action if the target changed. The result remains `verified: false` until Hermes independently checks it.

## Windows requirements

Skill selection and model routing work on Linux and Windows.

`jev_computer_use` requires Hermes on Windows and an installed target application. The application must be accessible through the normal Hermes computer-use path.

Before an action, approve the operation and supply the public-or-sanitized data confirmation. After an action, check the visible result independently.

Some text-entry actions use the configured Hermes text model. To select that model, run:

```text
hermes model
```

The Hermes text model is separate from Jev. It does not replace the OpenRouter account or key.

## Privacy

Do not send private, employer, or regulated data to these tools. Do not send passwords, credentials, API keys, tokens, payment details, or verification codes.

Before a Jev request, the tool input must include `public_or_sanitized_data_ack: true`. This flag states that the input was checked for permitted use. It does not scan or redact the input. It does not grant permission to share data or bypass another control.

For Windows actions, OpenRouter and Jev can receive:

- The goal and target application.
- The window title and visible control labels.
- Visible context and recent actions.

For text entry, the configured Hermes text model can also receive the goal, field details, visible context, and recent actions.

Never supply an API key through a command-line argument. Never put a key in a URL, repository file, test fixture, or issue report.

## Settings

Each Hermes profile has its own settings and secret scope. Plugin settings use `plugins.entries.jev-decision.settings`.

```text
hermes config set plugins.entries.jev-decision.settings.jev_model typesafe/jev-1.13
hermes config set plugins.entries.jev-decision.settings.computer_max_steps 12
```

The default model is `typesafe/jev-1.13`. The code also accepts one dated alias for compatibility.

Keep the default model unless a reviewed release specifies another value.

Switchyard sends Jev requests only to `https://openrouter.ai/api/alpha/decisions`. The code fixes `api_endpoint`, the accepted model aliases, and the provider fallback policy. It rejects other endpoints, including direct TypeSafe URLs.

## Limits

- A high confidence score does not prove that a choice is correct.
- Jev can decline to select an item. This result is called abstention.
- This version does not load skills, edit prompts, or change the active model.
- A completed tool call does not prove that a GUI task succeeded.
- A failed Jev request does not move to another provider.
- Offline tests use simulated responses. They do not call OpenRouter or control a real GUI.

Catalog admission, independent real-GUI coverage, and comparative evaluation remain future work. This release does not claim measured improvements.

## If the tools are missing

Without `OPENROUTER_API_KEY`, Hermes can disable the plugin at load time. If a tool handler runs without the key, it returns a generic request-validation error. It does not expose the key or provider credential details.

1. Supply the key through the masked installation prompt described earlier.
2. Start a fresh Hermes session.
3. Run `hermes plugins list --enabled`.
4. From the plugin root, run:

```text
hermes plugins doctor . --ci
```

Plugin Doctor imports and registers plugin code in the current process. It is not a sandbox. Run it only against code that you trust.

## Update or roll back

For an installation without a pinned commit, run:

```text
hermes plugins update jev-decision
```

An installation at an exact commit does not update automatically.

1. Select a reviewed commit from the [release instructions](docs/RELEASE.md).
2. Reinstall with `--force --ref` and that commit.
3. Check the result with `hermes plugins list` and `hermes plugins doctor . --ci`.
4. Enable the plugin if required.

These commands replace only the plugin in the active profile. They do not patch Hermes core.

Keep the previous reviewed commit SHA for rollback.

## Run the local checks

Offline checks require Python 3.11 or newer. Native plugin loading requires Hermes. The plugin has no other runtime Python dependencies.

```text
python -m unittest discover -s tests -v
python evaluation/evaluate.py --validate
python scripts/check_portability.py
```

Release archives use files from an exact Git commit. They include `SOURCE-MANIFEST.json` and `SHA256SUMS`. The builder extracts and checks each archive before it returns.

See the [release instructions](docs/RELEASE.md) for the complete procedure.

## Documentation

- [Setup](docs/SETUP.md)
- [Release instructions](docs/RELEASE.md)
- [Feature and test matrix](docs/TEST-MATRIX.md)
- [Contributing](CONTRIBUTING.md)
- [Security reporting](SECURITY.md)
- [Third-party references](THIRD_PARTY.md)
- [Changelog](CHANGELOG.md)

Original project work uses the MIT license. See `THIRD_PARTY.md` for upstream references.
