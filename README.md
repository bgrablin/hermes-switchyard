# Hermes Switchyard

Version: 0.5.6

**Switchyard makes the small decisions in a [Hermes Agent](https://github.com/NousResearch/hermes-agent) session faster and cheaper, and keeps a record of each one.**

Every turn, an AI agent makes small calls: *Which skill fits this request? Does "thanks!" really need maximum reasoning effort? Which button on this page should I click?* Switchyard hands those questions to [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev), a fast decision model that answers in a few hundred milliseconds. Code in the plugin then acts on the answer within limits you set, and writes a local receipt of what happened.

![Rail-yard map of Hermes Switchyard: bounded decisions pass local policy before Hermes acts](docs/assets/hermes-switchyard-overview.png)

The [Switchyard branding image](docs/assets/hermes-switchyard-branding.png) is also packaged.

## New here?

If these terms are unfamiliar, here is the 30-second version. The [Concepts primer](docs/CONCEPTS.md) has more.

- **Hermes Agent** is an open-source AI agent you chat with in your terminal. It uses a main language model of your choice and can run tools.
- **A plugin** is an add-on Hermes loads at startup. Switchyard is one.
- **Jev** is a separate, fast model from TypeSafe for small multiple-choice, scoring, and yes/no questions. You call it through **TypeSafe** or **OpenRouter** with your own API key. It is billed separately from any ChatGPT or Codex subscription.
- **A skill** is a packaged set of instructions (a `SKILL.md`) that teaches Hermes one kind of task.

## What Switchyard does for you

| Feature | What you'll notice | On by default? |
| --- | --- | --- |
| **Adaptive reasoning effort** | Easy turns like "hi" or "thanks" go out at lower effort. Your `/reasoning` level stays the maximum. On turns where Switchyard decides something, a short line under the reply shows what was sent. | Yes |
| **Automatic skill routing** | Hermes picks and loads the right skill from your catalog at the start of a turn, without you naming it. | Yes |
| **Computer use** | The main model can call `jev_computer_use` to click through a public web page in a fresh, private browser, or drive a desktop app. | Available as a tool |
| **Decision tools** | Tools for typed multiple-choice checks, multi-skill picks, model recommendations, and re-ranking past-session search. | Available as tools |
| **Source prefetch** | Ask "In notes.md, find the retry limit" and the exact passage is ready before Hermes answers. | No (opt-in pilot) |

Some things Switchyard deliberately does **not** do: it never switches your main model, never logs into websites for you, and never treats "Jev said done" as proof a task is finished.

## Quickstart

**You need:** a working Hermes install, one **TypeSafe** or **OpenRouter** API key, and to be comfortable sending **public or sanitized** task text to that provider. See [Privacy at a glance](#privacy-at-a-glance).

1. **Install** the plugin from GitHub. Hermes security-scans the source first:

   ```text
   hermes plugins install bgrablin/hermes-switchyard --no-enable
   ```

2. **Enable** it without letting it override Hermes' built-in tools:

   ```text
   hermes plugins enable hermes-switchyard --no-allow-tool-override
   ```

3. **Add your key.** You type it into a masked prompt, so never paste it into a command, chat, URL, or file:

   ```text
   hermes switchyard setup --provider typesafe
   # or: hermes switchyard setup --provider openrouter
   ```

4. **Start a fresh Hermes session**, then check that everything is wired up:

   ```text
   hermes switchyard status --json
   hermes plugins doctor hermes-switchyard --ci
   ```

   `status` should say `ready`. That means your key is present and the tools are visible. It does not make a Jev call.

5. **Optional: make one real (billed) test call** with public sample data:

   ```text
   hermes switchyard test --live --public-or-sanitized-data-ack
   ```

   This reports `passed` only after Jev actually answers.

**If something looks off:**

- **The install was blocked by a security scan.** Read the findings before you approve anything. A blocked install is not a success, and adding `--force` just to get past it defeats the point.
- **A running session doesn't see the plugin.** Sessions load plugins at startup. Start a new session, and run `hermes gateway restart` if you use the gateway.
- **Doctor passes but something still fails.** Plugin Doctor checks that the plugin is registered. It does not check your account, your key, or answer quality.
- **For anything else,** see [Troubleshooting](#troubleshooting) or the full [setup guide](docs/SETUP.md).

In testing, the local install steps took about 9 seconds ([first-run check](docs/FIRST-RUN.md)). Key entry and a first live answer were not timed.

## What you'll see

After a reply, Switchyard adds a short line showing the reasoning effort it actually sent. These two lines were recorded from a real session:

```text
Reasoning: high→low · local decision
Reasoning: kept at high — consequential request · 218 ms
```

- The first followed `hi`. It is obviously a greeting, so Switchyard lowered effort **on your machine**, with no Jev call.
- The second followed `thanks, now deploy`. Jev was asked, recognized a consequential request, and kept your full effort.

The timings are from one recorded session and are not a promise. The image below renders that recorded output; it is not a live screenshot:

![Rendering of recorded Switchyard receipt output for a greeting and a consequential request](docs/assets/effort-receipts-recorded.png)

Your status bar keeps showing your `/reasoning` level, which is the **cap**. The receipt line shows what was actually **sent** on each turn.

Useful commands:

| Command | Shows |
| --- | --- |
| `/switchyard effort status` | The last effort decision and why |
| `/switchyard effort summary` | Effort choices this session, with local and cloud counts |
| `/switchyard effort pin` / `auto` | Always send your level / let Switchyard lower it again |
| `hermes switchyard receipt --json` | The latest skill-routing receipt |
| `hermes switchyard stats` | Totals across recent turns |

A receipt records a decision. It does not prove the answer was correct or a browser task finished. More: [adaptive effort](docs/ADAPTIVE-REASONING-EFFORT.md) · [skill routing](docs/AUTOMATIC-INTEGRATION.md).

## Privacy at a glance

Jev is an external service. Here is what each feature sends to it:

| Feature | What goes to Jev | What never goes | How to keep it local |
| --- | --- | --- | --- |
| **Adaptive effort** (on) | Up to 1,200 characters of your **current message**, after secret scrubbing, plus simple status flags | Earlier messages, memory, tool output, files | `adaptive_reasoning_effort` → `false` |
| **Skill routing** (on) | Up to 4,000 characters of your **current message**, after secret scrubbing, plus your skill **names**. When Hermes supplies no policy for the turn, a local scan runs first; text Hermes explicitly authorizes skips that scan. If you opt in with `automatic_skill_hosted_detail`, also short descriptions or `SKILL.md` excerpts for a few finalists | Full skill bodies, history | `automatic_skill_routing_mode` → `local_only` |
| **Decision tools** | What the model passes to the tool. Only `jev_session_search_rerank` redacts anything (its result cards and previews, not the recall question) | — | `public_or_sanitized_data_ack` → `false` |
| **Computer use: browser** | The goal and a bounded view of the page (labels, visible text, recent steps). Values you ask it to type are masked out where they echo back (best-effort: a value copied into a host name or transformed by the page can slip through) | Your logins, cookies, files | Don't call the tool |
| **Computer use: desktop** | The goal, app and window title, control labels, visible context, and recent actions on every step. This is **not** masked, so typed values or signed-in app content can reappear | — | Only use it on public or sanitized apps and values |
| **Source prefetch** (off) | Your lookup question, plus text from 1–8 files you name (80,000 bytes max in total). If the scrubber would change anything, nothing is sent and Hermes handles the request normally | Anything outside the folder you approve | Leave it off |

Some turns never reach Jev:

- Obvious greetings and thanks are decided on your machine.
- When Hermes supplies no policy for the turn, messages with document markings such as `proprietary`, `company confidential`, or a standalone `Confidential` banner stay local.
- If your Hermes version lacks its secret scrubber, skill routing falls back to local matching and adaptive effort sends only message *shape* (length buckets and flags), no text.

**The fine print, in plain words:**

- Scrubbing catches *recognizable* secrets such as API keys, passwords in URLs, and auth headers. It is pattern-based and can miss new formats.
- Ordinary words like "password," and email addresses or phone numbers, are not treated as sensitive.
- The acknowledgement settings (`public_or_sanitized_data_ack`, `automatic_skill_public_or_sanitized_data_ack`) record *your* promise that inputs are public or sanitized. They are not a data-loss-prevention system.
- **Don't send secrets, credentials, payment data, or private or employer content** through Switchyard.

To make everything automatic stay on your machine:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false
```

Start a fresh session after changing settings. The full rules are in [setup](docs/SETUP.md) and [automatic routing](docs/AUTOMATIC-INTEGRATION.md).

## Supported features

The complete list of what Switchyard adds to Hermes:

| Surface | Available now | Default |
| --- | --- | --- |
| Tools (7) | `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, `jev_session_search_rerank`, `jev_computer_use` | Callable when the matching toolset is selected (see [Toolsets](#toolsets-and-session-exposure)) |
| Hooks (7) | `pre_llm_call` (skill routing and effort capture), `post_llm_call` (clears capture), `post_tool_call` (reconsiders effort), `transform_llm_output` (receipt line), `post_api_request` (token counts); optional `pre_tool_call` (requests approval) and `transform_tool_result` (output handling, stuck advice, and optional disposable exec soft-cap) | First five on after install; new tool hooks opt-in |
| Middleware (1) | `llm_request` sets the reasoning effort for each request | On when your Hermes version supports it (0.21.4+) |
| CLI | `hermes switchyard setup`, `status`, `test`, `receipt`, `stats`, `guide`, `ensure-toolsets` | Run when you want |
| CLI (offline, 0.6.0 candidates) | `hermes switchyard lint-skills [--json]` flags ambiguous descriptions ([guide](docs/LINT-SKILLS.md)); `hermes switchyard scan-catalog PATH` reviews package/MCP content with hashes and coverage gaps ([guide](docs/FEATURE-EXPANSION.md)) | Explicit only; no network, no edits |
| Local report (0.6.0 candidate; not in 0.5.6) | `hermes switchyard wow` and `/switchyard wow` summarize what Switchyard recorded locally ([guide](docs/WOW-LOCAL-REPORT.md)) | Last 7 days; read-only |

What each tool is for:

- **`jev_skill_select`** picks one skill from a catalog. **`jev_skill_select_many`** picks several for a multi-part task. Neither loads skills; only the automatic hook does that, one skill per turn.
- **`jev_assess`** asks Jev typed questions (multiple-choice, score, or yes/no) and validates the answers.
- **`jev_model_route`** and **`jev_model_route_approved`** *recommend* a model. They never switch it.
- **`jev_session_search_rerank`** reorders Hermes' past-session search results for recall questions.
- **`jev_computer_use`** drives a public web page or a desktop app, one Jev decision per step.

`hermes plugins doctor` reports six hook registrations by default, or seven with source prefetch enabled, because several handlers share `pre_llm_call`. The manifest lists seven distinct kinds, including two optional tool hooks. Jev `DONE` alone does not verify a browser goal; Switchyard also needs its local completion condition. An early local stop is a candidate, not verified success. [Browser receipts](docs/DOM-BROWSER-BACKEND.md) keep action, effect, and goal evidence separate.

### Automatic source prefetch (opt-in pilot)

Ask Hermes normally: "In notes.md, find the retry limit." With `evidence_finder_enabled` set to `true` and `evidence_finder_root` pointing at an approved folder, Switchyard finds the exact passage before Hermes' first model call. There's no slash command or tool to learn. If the request doesn't fit the pattern, or the evidence is uncertain, Hermes searches and reads files as usual. See [setup and limits](docs/SOURCE-FINDER.md).

## Automatic skill recommendations

At the start of an eligible turn, Switchyard looks at your active profile's skills and works out which one fits. By default it asks Jev, then loads the winner through Hermes' normal skill loader. You'll find the details in its receipt.

- **You stay in charge.** If you name a skill yourself, that wins and Switchyard stays out of the way.
- **It can say "none."** No good match, an unclear answer, or a failed check all mean no skill is loaded, which is normal.
- **Keep it local:** `automatic_skill_routing_mode=local_only` uses on-device word matching only.
- **Suggest, don't load:** `automatic_skill_consumer_mode=advisory`.

Neither setting affects your normal Hermes model calls or the explicit Jev tools. Gates and receipt fields: [routing details](docs/AUTOMATIC-INTEGRATION.md). Step-by-step setup: [automatic setup](docs/AUTOMATIC-SETUP.md).

## Computer use

`jev_computer_use` handles two kinds of goals:

- **Public web pages:** It opens a fresh, throwaway Chromium profile. It never attaches to your signed-in browser, never logs in, and never uploads files. If a form needs text, the model supplies the values in `text_inputs`. Jev picks *where* to type, and the values are never sent to it directly. Copies that echo back on the page are masked on a best-effort basis.
- **Desktop apps:** It uses Hermes' Cua Driver on supported systems.

When a run thinks it's finished, it returns a *completion candidate* with `verified: false`. The browser receipt has a stricter dual-gate verified state, but the current loop never reaches it; see the [browser guide](docs/DOM-BROWSER-BACKEND.md#action-evidence). The receipt shows whether it stopped because a local check passed (for example, "the URL equals …") or because Jev decided it was done. Either way, check the result yourself before treating it as done. Destination rules, receipts, and recovery: [browser and desktop guide](docs/DOM-BROWSER-BACKEND.md).

## Toolsets and session exposure

Hermes only lets a session call a plugin tool when that tool's **toolset** is turned on. Switchyard registers its seven tools under two toolsets:

| Toolset | Tools |
| --- | --- |
| `computer_use` | `jev_computer_use` |
| `hermes_switchyard` | `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, `jev_session_search_rerank` |

With Hermes' default selection, both are on, and `hermes switchyard setup` adds them to your CLI toolsets. One exception: if Hermes' coding focus mode (`agent.coding_context: focus`) is active, it overrides that list for sessions without `-t`. Check with `hermes switchyard status --json`. Watch out when you pin toolsets with `-t`: a pin **replaces** the defaults.

- Pinning only `computer_use` leaves out the six decision tools.
- Pinning only `hermes_switchyard` leaves out computer use.
- A pin listing only other toolsets exposes none of the seven tools.

To expose all seven in one CLI session:

```text
hermes -t computer_use,hermes_switchyard chat
```

`hermes switchyard status --json` tells you what a **fresh** session would get. It reports "registered" (Hermes knows the tool) and "callable" (the session can use it) separately. See [Confirm what a session exposes](docs/SETUP.md#confirm-what-a-session-exposes).

## Configuration

The defaults work for most people. These are the settings you're most likely to change:

| I want to… | Run |
| --- | --- |
| Keep skill routing on my machine | `hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only` |
| Get skill suggestions without auto-loading | `hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory` |
| Stop sending message text for effort decisions | `hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false` |
| Never lower my `/reasoning` level | `hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_mode pinned` |
| Hide the `Reasoning: …` line | `/switchyard effort receipt off` (saved for future sessions) |
| Soft-cap noisy exec tool stdout (opt-in) | `hermes config set plugins.entries.hermes-switchyard.settings.filter_disposable_tool_output true` |
| Choose a provider explicitly | `hermes config set plugins.entries.hermes-switchyard.settings.jev_provider typesafe` (or `openrouter`) |

Start a fresh session after any change. Every setting, grouped by feature and explained: [Configuration reference](docs/CONFIGURATION.md).

## Troubleshooting

`hermes switchyard status --json` and routing receipts report a **reason code** when something didn't happen. A reason code names the gate that stopped it; it isn't necessarily a bug:

| Reason | What it means | What to do |
| --- | --- | --- |
| `tools_not_registered` / `not_registered` | Hermes didn't register the plugin's tools. | Enable the plugin and start a fresh session. |
| `tools_not_callable` / `toolset_not_selected` | The tools are registered, but this session hasn't turned on their toolset. | Select `computer_use` and `hermes_switchyard`, and check `agent.disabled_toolsets`. |
| `credential_required` | No key for the provider Switchyard is set to use. | Run `hermes switchyard setup --provider …` for that provider. |
| `ack_required` | A public/sanitized acknowledgement setting is off. | Decide whether your input is public or sanitized first. Only then turn the setting back on. |
| `consumer_contract_unmet` | Skill routing is in `advisory` mode, so it won't pay for a Jev call. | Switch to `load` only if you want skills loaded automatically. |
| `local_scan_restricted_data` / `local_scan_restricted_marking` | The local scan found marked or restricted content, so the turn stayed local. | Keep this input local. Don't try to get around the scan with different wording. |
| `per_turn_policy_denied` / `per_turn_policy_invalid` | Hermes' own per-turn policy refused hosting, or sent something malformed. | Check the host policy. This turn stays local. |
| `client_unavailable` / `provider_request_failed` | No working Jev client, or the provider call failed. | Check your key, provider, and account balance, then read the receipt. Don't assume a fallback happened. |

**No `Reasoning: …` line under replies?**

- Start a fresh session after installing or changing settings.
- Check that the receipt mode isn't `off`: `/switchyard effort receipt auto`.
- In `auto` mode, the line only appears when Switchyard did something on that turn. Use `always` to also see it on pinned and pass-through turns where the sent level is known. Delegated, background, and unsupported-route turns stay quiet.
- It can't appear if adaptive effort is disabled or your Hermes is older than 0.21.4.

`/switchyard effort status` shows the current state. More status reasons, browser startup, and toolset pins: [setup guide](docs/SETUP.md).

## Uninstall and rollback

**Pause it** (keeps the installed files):

```text
hermes plugins disable hermes-switchyard
```

**Roll back:** `hermes plugins update hermes-switchyard` moves you *forward*, so it is not a rollback. To return to an earlier version, reinstall that exact reviewed Git commit through your normal security review.

**Remove it** once you no longer need its local data:

```text
hermes plugins remove hermes-switchyard
```

Before removing, save any receipts you want from the profile's `plugin-data/hermes-switchyard` folder. Start a fresh session afterwards. Don't delete a shared profile or gateway just to remove this plugin.

## What's in this release

This README describes the current `main` branch, which is version 0.5.6 plus work collected under **0.6.0 (unreleased)** in the [changelog](CHANGELOG.md).

**Version 0.5.6 includes:**

- local decisions for trivial turns
- the visible effort receipt line and session summary
- a 0.4-second default deadline for effort decisions

**Not included:** Research Navigator (F1) and DOM Progress & Recovery (F2). Both failed their release evaluations ([PR #132](https://github.com/bgrablin/hermes-switchyard/pull/132), [PR #135](https://github.com/bgrablin/hermes-switchyard/pull/135)). Don't install those PRs or copy their settings. Follow-up work is tracked in [issue #139](https://github.com/bgrablin/hermes-switchyard/issues/139).

**0.6.0 candidates not in 0.5.6:**

- [offline outcome labels](docs/OUTCOME-LABELS.md): local-only analysis of retained receipts. They add no hook and change no routing.
- the `wow` local report
- [automatic source prefetch](docs/SOURCE-FINDER.md) (opt-in pilot)
- `hermes switchyard lint-skills`

The [benchmark report](docs/BENCHMARKS.md) shows where Jev measurably helped (for example, picking the right skill 12/12 times against 7/12 for simple word matching) and is explicit about its limits. It does not claim Jev improves every task.

## Learn more

- [Documentation map](docs/README.md): every guide, sorted by what you want to do
- [Concepts primer](docs/CONCEPTS.md) · [Setup guide](docs/SETUP.md) · [Configuration reference](docs/CONFIGURATION.md)
- [Changelog](CHANGELOG.md) · [Release process](docs/RELEASE.md) · [Security](SECURITY.md) · [Third-party references](THIRD_PARTY.md) · [Contributing](CONTRIBUTING.md)
