# Hermes Switchyard

Jev-powered skill selection, model recommendations, and Windows computer use for Hermes Agent.

Version: 0.3.2

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is a model built for structured decisions. Switchyard brings it to Hermes: recommend a skill, choose a model from an approved list, or select the next action in a Windows application.

Jev supplies decision scores. Switchyard applies its eligibility and confidence rules; for model routing, it selects the cheapest qualified model. Hermes checks the result. This plugin uses Jev; it does not provide every feature that Jev supports.

![Hermes Switchyard H logo and HERMES SWITCHYARD wordmark](docs/assets/hermes-switchyard-branding.png)

Switchyard gives Hermes another way to choose among a defined set of options. It does not modify Hermes core, silently change the active model, load skills automatically, or claim that a recommendation or GUI action is correct.

## Install

### Quick install

The repository is private at present. Git must already have read access to the repository. If it does not, authenticate GitHub with `gh auth login` or configure a Git credential helper, then run this one-liner:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

The command installs and enables the plugin. To inspect the installed files before enabling it:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins list
hermes plugins enable jev-decision
```

Do not put a GitHub token in a clone URL, command, issue report, or repository file. Catalog installation is not available until a human admits the plugin to the Hermes catalog; use the repository command above.

After installing or updating, start a fresh Hermes session so it loads the new plugin. Restart only the Hermes process that needs to load the change.

## Setup requirements

GitHub access lets you download the plugin. To use Jev, you also need:

- An [OpenRouter](https://openrouter.ai/) account.
- An [OpenRouter API key](https://openrouter.ai/keys) stored in the active Hermes profile as `OPENROUTER_API_KEY`.
- Enough OpenRouter credit or current account allowance for the request.
- Access through OpenRouter to the approved Jev model, `typesafe/jev-1.13`.

ChatGPT and Codex subscriptions are separate from Jev. A Codex subscription pays for Codex usage. It does not pay Jev fees or OpenRouter requests. Jev uses your OpenRouter account and its credit or allowance.

Direct TypeSafe account or API access is not currently supported, and there is no direct TypeSafe adapter. The plugin uses only the fixed OpenRouter Decisions endpoint: `https://openrouter.ai/api/alpha/decisions`.

Use the secure setup steps in [docs/SETUP.md](docs/SETUP.md). Never pass an API key with a command-line argument or store it in a URL, repository file, fixture, or issue report.

## Supported features

- **Skill selection:** `jev_skill_select` recommends one skill from the candidate list supplied by Hermes. It never loads the skill; Hermes decides whether to load it.
- **Model routing:** `jev_model_route` filters candidates using the metadata and requirements supplied by Hermes, then recommends the lowest-cost qualified candidate. It never changes the active Hermes model and does not try another provider when Jev fails.
- **Windows computer use:** `jev_computer_use` runs bounded actions in a specified Windows application through Hermes' normal computer-use approval and action controls. It captures the target again before acting, refuses a changed target, and returns `verified: false`. Hermes must check the result separately; the tool does not certify success.

The default Jev model is `typesafe/jev-1.13`. The implementation also accepts one dated alias for compatibility, but users should keep the default unless a reviewed release gives a different value. The endpoint, model aliases, and provider fallback policy are fixed in code.

## Privacy and data handling

Jev tools require `public_or_sanitized_data_ack: true` in their input. This means the input has been checked for permitted use. The flag does not scan or redact data, grant permission to share it, or bypass other controls.

For Windows computer use, Jev may receive the goal, target application, window title, safe visible control labels, visible context, and recent actions through OpenRouter. When the loop enters text, the configured Hermes text model may also receive the goal, field details, visible context, and recent actions. Do not send private, employer, regulated, credential, password, API-key, token, payment, or verification-code data.

## Tools and limits

The tools are advisory and bounded:

- A high confidence score is not proof that a choice is correct.
- Switchyard can return no selection when eligibility or confidence checks fail. This is called abstention.
- The plugin does not load skills, edit prompts, change runtime models, or certify GUI completion.
- Provider fallback is disabled. A failed Jev request does not silently move to another provider.
- Skill selection and model routing work on Linux and Windows. `jev_computer_use` is available only when Hermes runs on Windows.
- The repository's offline tests use synthetic transports and do not call OpenRouter or drive a real GUI.

Future work includes a reviewed catalog admission, independent real-GUI coverage, and comparative evaluation. Those are not provided by this release.

## Safe credential setup

The installation step requests `OPENROUTER_API_KEY` through a masked prompt when the key is missing. Hermes stores it in the active profile. If you already supplied the key, there is no need to reinstall.

Do not use `hermes auth add openrouter` for this plugin. That command manages a provider credential pool; Switchyard reads the profile-scoped `OPENROUTER_API_KEY` secret instead. Check the enabled plugin without displaying the key:

```text
hermes plugins list --enabled
```

If Hermes reports that `OPENROUTER_API_KEY` is missing, rerun `hermes plugins install bgrablin/hermes-switchyard --force --enable` and follow the manifest's masked prompt, then start a fresh Hermes session and check the plugin list again. Logging in to Codex or changing `jev_model` does not fix a missing OpenRouter key.

## Windows prerequisites

Skill selection and model routing do not require Windows. `jev_computer_use` requires Hermes to run on a Windows host with the target application installed and available to Hermes' normal computer-use path. The operation needs approval and confirmation that its input is public or sanitized. The loop does not prove that the application task completed; verify the visible result independently.

Some text-entry actions use the host-owned Hermes text model. Select a configured Hermes model with the normal interactive command when needed:

```text
hermes model
```

This host model is separate from the OpenRouter Jev model and does not replace the OpenRouter account or key.

## Configuration

Settings are profile-scoped under `plugins.entries.jev-decision.settings`:

```text
hermes config set plugins.entries.jev-decision.settings.jev_model typesafe/jev-1.13
hermes config set plugins.entries.jev-decision.settings.computer_max_steps 12
```

`api_endpoint` is fixed by the implementation. Changing it to a TypeSafe URL or another provider is rejected. Each Hermes profile has its own settings and secret scope.

## Missing-key symptoms

When `OPENROUTER_API_KEY` is absent, Hermes can disable the plugin during loading and the Jev tools will not be available. If a handler is reached without a key, the plugin fails closed with a generic request-validation error; it does not print the key or provider credential details.

The supported recovery is:

1. Add the key through the masked plugin installation prompt described above.
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
hermes plugins update jev-decision
```

An exact-SHA install does not move implicitly. Reinstall with `--force --ref` and the reviewed commit described in [docs/RELEASE.md](docs/RELEASE.md), then enable the plugin if required. Check the result with `hermes plugins list` and `hermes plugins doctor . --ci` before enabling it.

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

Own work is MIT-licensed. See `THIRD_PARTY.md` for conceptual upstream references.