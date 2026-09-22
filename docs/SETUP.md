# Setup

Hermes Switchyard has two separate setup boundaries:

1. Network access to download the public GitHub repository; no GitHub login or token is required.
2. TypeSafe or OpenRouter access for live Jev decisions.

A ChatGPT or Codex subscription is separate and does not pay Jev or OpenRouter request charges.

## Requirements

For skill selection, model routing, and `jev_assess`:

- Hermes Agent with the native plugin contract: `plugin.yaml`, a root `__init__.py`, and `register(ctx)`.
- Python 3.11 or newer for the repository's offline checks.
- Network access to `github.com/bgrablin/hermes-switchyard`.
- Either a TypeSafe API key in the active profile as `TYPESAFE_API_KEY`, or an OpenRouter API key as `OPENROUTER_API_KEY`.
- Enough account credit or current allowance for the selected route.

For `jev_computer_use`, public web goals use a local Chromium-family browser and do not call Hermes `computer_use` between clicks. Desktop GUI goals still need the Cua Driver-backed `computer_use` tool on Windows, macOS, or Linux.

The supported endpoints are `https://api.typesafe.ai/v1/systemone` and `https://openrouter.ai/api/alpha/decisions`. `jev_provider: auto` prefers direct TypeSafe when its key exists.

## Install

The repository is public. No GitHub login or token is required. Do not put a token in a Git URL.

Install and enable the plugin with the supported one-liner:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

Both provider keys are optional alternatives, so installation does not prompt for either one. To install without enabling first:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins list
hermes plugins enable hermes-switchyard
```

Start a fresh Hermes session after installation or an update.

## Add a Jev key safely

Run exactly one provider-specific setup command and enter the key only in its masked prompt:

```text
hermes switchyard setup --provider typesafe
# or
hermes switchyard setup --provider openrouter
```

Do not put a key in `hermes auth add`, a command argument, URL, fixture, repository file, or issue report. Check plugin availability without displaying keys:

```text
hermes plugins list --enabled
```

Profiles do not share secrets automatically. After adding or changing a key, start a fresh Hermes session.

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

A successful native check proves discovery and registration, not model quality, GUI completion, or permission to send private data. It also does not prove that a session can call a tool.


## Adaptive reasoning effort (Hermes ≥ 0.21)

After install, adaptive reasoning effort is **on**. Switchyard registers Hermes
`llm_request` middleware and asks Jev for a typed Choice over
`none|minimal|low|medium|high|xhigh|max|ultra` before each generation (and again
after tools when outcomes change). The middleware rewrites only request-scoped
effort fields — messages stay untouched for prompt-cache friendliness. If Jev
fails or ack is missing, the previous effort is kept.

Disable:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false
```

Optional defaults:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_default medium
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_deadline_seconds 8
```

On Hermes hosts without `register_middleware`, registration records
`noop_seam_unavailable` and does not change requests. `jev_model_route` remains
advisory (`applied: false`); adaptive effort is the apply path.

## Confirm what a session exposes

Hermes puts a tool in a session's callable catalog only when the toolset the tool is registered under is selected for that session. The required composition is:

- `jev_computer_use` is exposed only when the `computer_use` toolset is selected. Hermes' own `computer_use` tool is in the same toolset.
- `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, and `jev_model_route` are exposed only when the `hermes_switchyard` toolset is selected.
- A session started without `--toolsets` uses Hermes' default CLI selection, which includes both toolsets under a default configuration. A toolset list saved by `hermes tools` that leaves Computer Use off keeps `jev_computer_use` out of sessions.
- An explicit `--toolsets` (`-t`) pin replaces the default selection and does not add plugin toolsets. To expose all five tools, name both: `hermes -t computer_use,hermes_switchyard chat`. In PowerShell, quote the list, because an unquoted comma is PowerShell's array operator.
- Hermes also subtracts the configured `agent.disabled_toolsets` list from every CLI session, including one with an explicit pin. A required toolset named there stays unreachable whatever `--toolsets` says, so naming both toolsets is not enough while either is listed. To clear the suppression, remove the name from `agent.disabled_toolsets` in `config.yaml`, or enable the toolset in `hermes tools`, which also removes it from that list for the CLI. `status` reads the same list, so it reports the suppression instead of a false `callable`.

Registered and callable are different facts. Registered means Hermes' registry holds this plugin's own registration for the tool. Callable means the tool is in the catalog Hermes builds for a session with a given toolset selection. Hermes Plugin Doctor reports discovery/import/registration only and does not evaluate per-session callable exposure. The plugin's own status command reports both, with no network access. `status --json` also includes a `toolset_composition` object that names the required toolsets and repeats that Doctor boundary. To add `computer_use` and `hermes_switchyard` to `platform_toolsets.cli` without enabling unrelated toolsets, run `hermes switchyard ensure-toolsets` (also invoked from `setup` after saving a key):

```text
hermes switchyard status --json
hermes switchyard status --json --toolsets computer_use,terminal
```

The first form evaluates the toolsets Hermes' CLI uses for a session started without `--toolsets`. The second evaluates an explicit pin, so you can reproduce what a scripted launch will expose. `status` evaluates a fresh session with Hermes' own catalog builder. It does not read the catalog of a session that is already running, so start a fresh session after changing the plugin, its configuration, or the toolsets. The command exits with status 0 in every state; read the JSON.

The `status` field names the first problem found, in this order:

| `status` | Meaning | What to do |
| --- | --- | --- |
| `tools_not_registered` | Hermes' registry does not hold this plugin's registration for at least one tool. | Read that tool's `reason`. |
| `tools_not_callable` | Every tool is registered, but at least one is missing from the evaluated session catalog. | Read that tool's `reason`. |
| `credential_required` | The tools are registered and callable, but the key for the provider the configured route uses (`effective_provider`) is missing. A key for the other provider does not count: an explicit `jev_provider` or `api_endpoint` can select OpenRouter while only a TypeSafe key exists, or the reverse. | Run `hermes switchyard setup --provider typesafe` or `--provider openrouter` for the effective provider, or point `jev_provider` at the provider whose key exists. |
| `exposure_unverified` | The effective provider's key exists, but Hermes' registry, catalog, or configured suppression list could not be read, so callability is unknown. | See `tool_exposure.unavailable_reason`. Nothing is assumed available. |
| `ready` | The effective provider's key exists and all five tools are registered and callable in the evaluated selection. | None. |

`tool_exposure.tools` lists each tool with `expected_toolset`, `registered`, `registry_toolset`, `callable`, and `reason`. A `null` value means it could not be determined, and it is never treated as available. `tool_exposure.selection` names the evaluated `source` (`explicit_toolsets`, `platform_default`, or `coding_posture`), the `enabled_toolsets`, the `disabled_toolsets` read from `agent.disabled_toolsets`, and any `unknown_toolsets`, which are names Hermes ignores, such as a misspelled `computer-use`. `effective_provider` is `typesafe` or `openrouter`: with `jev_provider: auto` the route uses TypeSafe when its key exists and OpenRouter otherwise. It is `null` when the route is invalid or the plugin did not register in this process, and `status` then accepts a key for either provider.

| `reason` | Stage | Meaning | Fix |
| --- | --- | --- | --- |
| `not_registered` | registration | Hermes' registry has no entry for the tool. | Enable the plugin with `hermes plugins enable hermes-switchyard`, then start a fresh session. |
| `owned_by_another_registration` | registration | The registry holds an entry under that name that this plugin did not register. Hermes rejects a second registration of a name that already sits in a different toolset, and it does so without raising an error, so a duplicate or legacy copy of the plugin, such as an install from before the rename to Hermes Switchyard, can hold the name. `registry_toolset` names the toolset that owns it. | Remove the duplicate or legacy copy and start a fresh session. |
| `toolset_not_selected` | exposure | The selected toolsets do not include `registry_toolset`. | Add that toolset to `--toolsets`, or enable it in `hermes tools`. |
| `toolset_disabled` | exposure | `registry_toolset` is listed in `agent.disabled_toolsets`, which Hermes subtracts last, even from an explicit pin. It is reported ahead of `toolset_not_selected`, because adding the toolset to `--toolsets` cannot help. | Remove it from `agent.disabled_toolsets` in `config.yaml`, or enable it in `hermes tools`. |
| `availability_check_failed` | exposure | The toolset is selected, but the tool's own availability check returned false. For the decision tools that means an invalid or contradictory `jev_provider` or `api_endpoint`. For `jev_computer_use` it means an unsupported platform. | Correct the setting the check reads, then start a fresh session. |
| `not_in_catalog` | exposure | The toolset is selected and the check passes, yet Hermes left the tool out. | Open an issue with the `status --json` output. The plugin cannot see Hermes' reason. |

## Configure the plugin

The plugin settings are profile-scoped under `plugins.entries.hermes-switchyard.settings`:

```text
hermes config set plugins.entries.hermes-switchyard.settings.jev_provider auto
hermes config set plugins.entries.hermes-switchyard.settings.computer_max_steps 100
```

Leave `jev_model` empty to use the provider default. Direct TypeSafe uses `jev-latest`; OpenRouter uses `typesafe/jev-1.13`.

If an earlier setup pinned the TypeSafe-only alias while using `auto`, remove it so provider-specific defaults work:

```text
hermes config unset plugins.entries.hermes-switchyard.settings.jev_model
```

Text-entry and value-selection actions use only bounded caller-supplied values from `text_inputs`; the registered `jev_computer_use` tool never calls a conversational Hermes LLM to compose field text between Jev actions. If a text action needs a value, supply it in `text_inputs` before the operation. Jev and the configured host model are separate: Jev makes the decision, and the host model is used elsewhere. That model is separate from Jev. A Codex login can supply Hermes' host model when configured, but it does not supply a TypeSafe or OpenRouter account, key, credit, or Jev access.

## Privacy requirements

`public_or_sanitized_data_ack` is on after install. Callers may omit it. Pass `false` to refuse one call, or set `plugins.entries.hermes-switchyard.settings.public_or_sanitized_data_ack` to false to refuse all Jev tools. Hermes owns data classification. The flag is not a scan, redaction guarantee, DLP control, or permission to bypass another control.

For a Cua Driver request, Switchyard builds a bounded decision state from the goal, target application, window title, safe controls, visible context, and recent actions. Text entry can also use selected field context with the configured Hermes text model. The caller must exclude private, employer, regulated, credential, password, API-key, token, payment, and verification-code data before invocation.

## What is supported

- `jev_assess` validates public/sanitized Choice, Score, and Noul answers and batches large independent question maps into bounded requests.
- `jev_skill_select` searches the full supplied catalog through partitioned Choices. It does not load the skill.
- `jev_model_route` filters and ranks explicit candidate metadata. It does not change the active model or use a fallback provider.
- `jev_computer_use` runs bounded actions through Hermes' Cua Driver-backed tool on Windows, macOS, and Linux. It rechecks targets before acting and returns `verified: false` until Hermes independently checks the result.
- Jev may abstain. A confidence value is not a correctness guarantee.

## Missing-key symptoms and recovery

If neither `TYPESAFE_API_KEY` nor `OPENROUTER_API_KEY` is available, the tools still appear after enablement. Calls fail closed until you save one key. Install prints `after-install.md`; `hermes switchyard guide` reprints it.

Recover without changing code:

1. Run `hermes switchyard setup --provider typesafe` or use `--provider openrouter` and enter the key only in the masked prompt.
2. Start a fresh Hermes session.
3. Run `hermes plugins list --enabled`.
4. Run `hermes plugins doctor . --ci` from the plugin root if discovery remains unclear.

A Codex login, a different `jev_model` value, or a missing direct key does not fix a missing secret. Choose the provider whose profile secret is present.

## Limits and future work

Automatic skill routing defaults to hosted_sanitized + load with standing acknowledgement after install. The load path invokes Hermes' normal `skill_view` loader once for an accepted identified turn; explicit skill instructions, abstention, invalid output, and loader rejection suppress the automatic load. Opt down to `local_only` / `advisory` for privacy. The release does not change the active Hermes model, use provider fallback, claim calibrated correctness, or certify GUI completion independently. Cua Driver remains the host-owned desktop executor; Switchyard adds the Jev decision layer and does not bypass its approval or platform boundaries.
