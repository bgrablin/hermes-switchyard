# Setup guide

This guide takes you from nothing to a working Switchyard, and explains how to check that a Hermes session can actually use it. If you just want the short version, the [README quickstart](../README.md#quickstart) has it. If terms like "toolset" or "profile" are new, skim the [Concepts primer](CONCEPTS.md) first.

**Overview:**

1. Install and enable the plugin. No GitHub account is needed.
2. Save one Jev key, for TypeSafe or OpenRouter.
3. Start a fresh session and confirm the tools are visible.

## Before you start

You need:

- **Hermes Agent** with native plugin support. Adaptive reasoning effort also needs Hermes **0.21.4 or newer**. On older versions, effort adaptation just stays inactive.
- **Network access** to `github.com/bgrablin/hermes-switchyard`. The repository is public, so no token is needed. Never put a token in a Git URL.
- **One Jev API key:** either `TYPESAFE_API_KEY` (direct TypeSafe) or `OPENROUTER_API_KEY` (OpenRouter), with enough credit on that account.
- **For browser goals:** a Chromium-family browser (Chrome, Chromium, or Edge). **For desktop apps:** Hermes' Cua Driver-backed `computer_use` tool on Windows, macOS, or Linux.
- **For contributors only:** Python 3.11+ to run the repository's offline checks.

> **Billing note:** Jev calls are billed by TypeSafe or OpenRouter. A ChatGPT or Codex subscription does not pay for them, even if Codex supplies your main Hermes model.

## Step 1: Install and enable

The safest path installs first, then enables without allowing the plugin to override Hermes' built-in tools:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins enable hermes-switchyard --no-allow-tool-override
```

If you prefer one command:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

Hermes security-scans the source during install. If the scan blocks the install, read the findings. Don't use `--force` just to get past it.

Installation does not ask for a key, because either provider works. You'll add one next.

> **Already have Hermes open?** Running sessions and TUI windows keep the plugin code they started with. Close and reopen them after installing or updating.

## Step 2: Add a Jev key

Run **one** of these and type the key into the masked prompt:

```text
hermes switchyard setup --provider typesafe
# or
hermes switchyard setup --provider openrouter
```

Setup also runs `hermes switchyard ensure-toolsets`, which adds the `computer_use` and `hermes_switchyard` toolsets to your CLI toolset list (`platform_toolsets.cli`) without enabling anything else. If Hermes' coding focus mode (`agent.coding_context: focus`) is active, it takes precedence for sessions started without `-t`. In that case, pin the toolsets with `-t`, or leave focus mode, and confirm with `hermes switchyard status --json` (it reports `source: coding_posture`).

Never put a key in `hermes auth add`, a command argument, a URL, a test fixture, a repository file, or an issue report.

Keys belong to a **profile**. If you use several Hermes profiles, run setup in each one. After adding or changing a key, start a fresh session.

## Step 3: Start fresh and check

Start a new Hermes session, then:

```text
hermes plugins list --enabled          # is the plugin enabled?
hermes switchyard status --json        # is a key present, and can a session call the tools?
```

`status` runs locally with no network access and never prints your key. Its `status` field should be `ready`. If it says something else, see [Confirm what a session exposes](#confirm-what-a-session-exposes) below.

**Optional: test the plugin loader without touching your real profile.** Run Plugin Doctor in a throwaway Hermes home:

```text
HERMES_HOME="$(mktemp -d)" hermes plugins doctor . --ci
```

On Windows, set `HERMES_HOME` to a new temporary folder using your shell's syntax.

Plugin Doctor really imports and runs the plugin's registration code. It is not a sandbox, so only run it on code you've reviewed. A pass proves the plugin loads and registers. It does **not** prove answer quality, browser success, permission to send private data, or that a particular session can call the tools.

## Confirm what a session exposes

Two separate facts decide whether a session can use a Switchyard tool:

- **Registered:** Hermes' registry holds this plugin's registration for the tool. Plugin Doctor checks this.
- **Callable:** the tool is in the catalog Hermes builds for *this* session. That depends on which toolsets are selected. Only `hermes switchyard status` checks this.

### Which toolset holds which tool

- `jev_computer_use` is in the **`computer_use`** toolset, next to Hermes' own `computer_use` tool.
- `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, and `jev_session_search_rerank` are in the **`hermes_switchyard`** toolset.

### How Hermes picks toolsets for a session

- **No `--toolsets` flag:** Hermes uses its default CLI selection. Under a default configuration, that includes both toolsets. If you saved a toolset list with `hermes tools` that leaves Computer Use off, `jev_computer_use` stays out.
- **With `--toolsets` / `-t`:** your list **replaces** the defaults. It does not add to them. To get all seven tools, name both:

  ```text
  hermes -t computer_use,hermes_switchyard chat
  ```

  In PowerShell, quote the list (`-t "computer_use,hermes_switchyard"`), because an unquoted comma means something else there.
- **`agent.disabled_toolsets` always wins.** Hermes subtracts this list from every CLI session, even one with an explicit pin. To undo it, remove the name from `agent.disabled_toolsets` in `config.yaml`, or enable the toolset in `hermes tools`.

### Checking with `status`

```text
hermes switchyard status --json                                    # a session started with no -t flag
hermes switchyard status --json --toolsets computer_use,terminal   # a session started with that exact pin
```

`status` asks Hermes' own catalog builder what a **fresh** session would get. It cannot see inside a session that's already running, so restart after changing anything. It always exits with code 0, so read the JSON. `status --json` also includes a `toolset_composition` object that names the required toolsets.

The top-level `status` field names the **first** problem it found:

| `status` | Meaning | What to do |
| --- | --- | --- |
| `tools_not_registered` | Hermes doesn't hold this plugin's registration for at least one tool. | Read that tool's `reason` (table below). |
| `tools_not_callable` | Everything is registered, but at least one tool isn't in this session's catalog. | Read that tool's `reason`. |
| `credential_required` | Tools are fine, but there's no key for the provider Switchyard will actually use (`effective_provider`). A key for the *other* provider doesn't count. | Run `hermes switchyard setup --provider …` for the effective provider, or set `jev_provider` to the provider whose key you have. |
| `exposure_unverified` | A key exists, but Hermes' registry, catalog, or disabled list couldn't be read, so callability is unknown. | See `tool_exposure.unavailable_reason`. Nothing is assumed to work. |
| `ready` | Key present, and all seven tools registered and callable. | Nothing. You're set. |

**Which provider is "effective"?** With `jev_provider: auto`, it's TypeSafe if a TypeSafe key exists, otherwise OpenRouter. If you set `jev_provider` or `api_endpoint` explicitly, that wins. `effective_provider` is `null` when the route is invalid or the plugin didn't register in this process; `status` then accepts a key for either provider.

Each entry in `tool_exposure.tools` has `expected_toolset`, `registered`, `registry_toolset`, `callable`, and `reason`. A `null` means "couldn't tell," and is never treated as available. `tool_exposure.selection` shows what was evaluated:

- `source`: `explicit_toolsets`, `platform_default`, or `coding_posture`
- `enabled_toolsets`
- `disabled_toolsets`: read from `agent.disabled_toolsets`
- `unknown_toolsets`: names Hermes ignores, such as a misspelled `computer-use`

Per-tool `reason` codes:

| `reason` | Stage | Meaning | Fix |
| --- | --- | --- | --- |
| `not_registered` | registration | Hermes has no entry for the tool. | `hermes plugins enable hermes-switchyard`, then start a fresh session. |
| `owned_by_another_registration` | registration | Another registration already owns that tool name, usually a duplicate or pre-rename legacy copy of this plugin. Hermes silently refuses the second registration. `registry_toolset` names the owner. | Remove the duplicate or legacy copy, then start a fresh session. |
| `toolset_not_selected` | exposure | The session's toolsets don't include `registry_toolset`. | Add it to `--toolsets`, or enable it in `hermes tools`. |
| `toolset_disabled` | exposure | The toolset is in `agent.disabled_toolsets`, which overrides even an explicit pin. This is reported before `toolset_not_selected`, because adding it to `--toolsets` won't help. | Remove it from `agent.disabled_toolsets`, or enable it in `hermes tools`. |
| `availability_check_failed` | exposure | The toolset is selected, but the tool's own availability check returned false. For decision tools, `jev_provider` or `api_endpoint` is invalid. For `jev_computer_use`, the platform is unsupported. | Fix the setting, then start a fresh session. |
| `not_in_catalog` | exposure | Selected and available, yet Hermes still left the tool out. | Open an issue with your `status --json` output. The plugin can't see Hermes' reason. |

If tools go missing after setup, re-run `hermes switchyard ensure-toolsets`.

## Choosing a provider and model

Switchyard talks to exactly two fixed endpoints:

- Direct TypeSafe: `https://api.typesafe.ai/v1/systemone`
- OpenRouter: `https://openrouter.ai/api/alpha/decisions`

With the default `jev_provider: auto`, TypeSafe is preferred when its key exists. To choose explicitly:

```text
hermes config set plugins.entries.hermes-switchyard.settings.jev_provider auto
```

Leave `jev_model` empty to get each provider's default: `jev-latest` on TypeSafe, `typesafe/jev-1.13` on OpenRouter. If an older setup pinned the TypeSafe-only alias while using `auto`, clear it:

```text
hermes config unset plugins.entries.hermes-switchyard.settings.jev_model
```

**Jev is not your main model.** Jev makes the small decisions, and your configured Hermes model does everything else. A Codex login can supply Hermes' main model, but it does not give you TypeSafe or OpenRouter access, a key, or credit.

All other settings: [Configuration reference](CONFIGURATION.md).

## Adaptive reasoning effort, briefly

After install, adaptive effort is **on** in `auto` mode:

- **Your `/reasoning` level is the cap.** Jev may pick a lower level for routine steps. It never goes higher unless you enable `adaptive_reasoning_effort_allow_raise`, and then only by one level after a failed tool call.
- **What Jev sees:** a bounded, scrubbed excerpt of your **current message** only. It never sees history, memory, plugin context, or tool results.
- **Timing:** Jev gets 0.4 s by default (adjustable from 0.1 to 1.5 s). On timeout or any failure, your level is sent unchanged.
- **Changing `/reasoning` mid-session** sets a new cap. It does not pin the session.
- **Prompt caching is unaffected:** only the effort field of each request changes, never the messages.

Session commands:

```text
/switchyard effort status | pin | auto
```

Common settings:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_mode pinned
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_exclude_models '["*astra*"]'
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_allow_raise true
```

On a Hermes without `register_middleware`, the feature records `noop_seam_unavailable` and changes nothing. Full behavior: [ADAPTIVE-REASONING-EFFORT.md](ADAPTIVE-REASONING-EFFORT.md).

## What each tool does

- **`jev_assess`** asks Jev typed questions, Choice (pick one), Score (rate on a scale), or Noul (yes/no), and validates the answers. Large sets of independent questions are split into bounded batches.
- **`jev_skill_select`** searches the whole catalog you give it and returns the best skill. It does **not** load the skill.
- **`jev_model_route`** filters and ranks model candidates you supply. It does **not** change the active model or use a fallback provider.
- **`jev_computer_use`** runs bounded steps toward a goal: in a fresh browser for public web pages, or through Hermes' Cua Driver for desktop apps. It rechecks targets before acting. A finished run is a *completion candidate* with `verified: false`, until something else independently checks the result.

Jev may abstain. A confidence number is not a correctness guarantee.

For text fields, `jev_computer_use` uses only values the caller supplies in `text_inputs`. It never asks a chat model to make up field text between steps, so supply any needed values up front.

## Privacy requirements

`public_or_sanitized_data_ack` is on after install, so callers don't have to pass it. To refuse a single call, the caller passes `false`. To refuse all Jev tool calls, set the setting to `false`.

This flag is your attestation, not a scanner. It doesn't redact anything, doesn't act as DLP, and doesn't override any other control. Hermes owns data classification.

For desktop computer use, Switchyard builds a bounded snapshot from the goal, target app, window title, safe controls, visible context, and recent actions. Text entry types only the values the caller supplies in `text_inputs`. No Hermes text model is asked to compose field text. **Before calling it, make sure none of that contains** private, employer, regulated, credential, password, API-key, token, payment, or verification-code data.

## If your key is missing

The tools still appear, but every call fails safely until you add a key. Install prints `after-install.md`; `hermes switchyard guide` shows it again.

To fix it:

1. Run `hermes switchyard setup --provider typesafe` (or `--provider openrouter`) and enter the key in the masked prompt.
2. Start a fresh Hermes session.
3. Run `hermes plugins list --enabled`.
4. If discovery still looks wrong, run `hermes plugins doctor . --ci` from the plugin folder.

A Codex login, a different `jev_model`, or a key for the *other* provider won't fix a missing key. Use the provider whose key you actually saved.

## Limits

- Automatic skill routing is on by default (`hosted_sanitized` + `load`). It loads at most one accepted skill per turn through Hermes' normal `skill_view` loader. Naming a skill yourself, an abstention, an invalid answer, or a loader rejection all prevent the automatic load. For more privacy, switch to `local_only` or `advisory`.
- Switchyard never changes your active Hermes model, never uses provider fallback, and doesn't claim calibrated correctness or independently certify that a GUI task finished.
- For desktop work, Cua Driver remains Hermes' own executor. Switchyard adds the decision layer on top and does not bypass Cua Driver's approvals or platform limits.

## Optional: automatic source prefetch

To have Switchyard fetch an exact passage from a file you name before Hermes answers, see [SOURCE-FINDER.md](SOURCE-FINDER.md). It adds no tool and needs no toolset.
