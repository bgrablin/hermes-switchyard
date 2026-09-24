# Hermes Switchyard

Hermes Switchyard helps [Hermes Agent](https://github.com/NousResearch/hermes-agent) choose a relevant skill, adjust reasoning effort, and work through browser or desktop tasks. It uses [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) for bounded decisions. Hermes remains in charge of actions and the final result.

![Rail-yard map of Hermes Switchyard: Jev decisions pass policy before Hermes acts; typed assessment, skill selection and loading, adaptive effort, model advice, computer use, session re-ranking, and receipts are distinct capabilities](docs/assets/hermes-switchyard-overview.png)

Version: 0.5.3

Switchyard can:

- **Find skills:** recommend one skill or several for the same task. `jev_skill_select_many` returns a set of recommendations. Separately, the default automatic hook can load one accepted skill for the current turn and choose another on a later turn.
- **Adjust reasoning effort:** choose an effort level for each request, and reconsider it after tool results. If Jev makes no usable decision, Switchyard keeps the existing effort.
- **Recommend a model:** compare candidates you supply or read a profile-approved registry, then leave a receipt. It does **not** switch the active model.
- **Guide computer use:** choose browser or desktop actions one step at a time. A proposed finish is not proof that the task is complete.
- **Assess or re-rank:** answer a bounded typed question or re-rank a shortlist from Hermes session search.

These features need a Jev provider key for live decisions. They can incur separate provider charges; a ChatGPT or Codex subscription does not cover Jev requests. The [benchmark report](docs/BENCHMARKS.md) has measured results, comparison arms, and limits. It does not claim that every Hermes task improves.

## Get started

Install and enable the public repository. You do not need a GitHub login or token:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

Save **one** Jev key through a masked prompt. Use the provider you have:

```text
hermes switchyard setup --provider typesafe
# or
hermes switchyard setup --provider openrouter
```

Start a fresh Hermes session, then inspect the plugin without showing your key:

```text
hermes switchyard status --json
```

TypeSafe and OpenRouter are separate paid or allowance-based routes. With `jev_provider: auto`, Switchyard prefers TypeSafe when its profile key exists; otherwise it uses OpenRouter. Do not put a key in a command argument, Git URL, repository file, or issue report. See [setup](docs/SETUP.md) for requirements, tool exposure, and troubleshooting. To inspect the plugin before enabling it, install with `--no-enable` instead of `--enable`.

## Privacy and control

**Two automatic features are on after installation.** They have different data boundaries:

- **Skill routing** can send a bounded task excerpt and exact candidate identifiers to the selected Jev provider when its hosted path is allowed. It does not send full skill bodies or the conversation history. A local restricted-data scan is not a guarantee that private data was removed.
- **Adaptive reasoning effort** can send task-presence and length metadata, the prior effort, and recent tool status codes. It does not send raw user-message text or tool-result excerpts on that path.

Do not use hosted Jev decisions for credentials, payment or verification codes, employer data, or other private or regulated content. Hermes owns data classification. Switchyard does not change the active model, silently try another provider, or bypass Hermes' computer-use controls. Its computer-use tool can send a goal and safe visible context to Jev; do not use it for private or authenticated screens. Read the [automatic-routing boundary](docs/AUTOMATIC-INTEGRATION.md) and [computer-use boundary](docs/DOM-BROWSER-BACKEND.md) before using those paths.

To keep automatic skill matching local and stop it from loading a selected skill, set both options:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory
```

To disable adaptive effort:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false
```

The [setup guide](docs/SETUP.md) covers provider selection and further privacy settings. Start a fresh session after a configuration change.

## Supported features

| Surface | What is available | Default |
| --- | --- | --- |
| Tools (7) | `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, `jev_session_search_rerank`, `jev_computer_use` | Callable when the corresponding toolset is selected |
| Hooks (2) | `pre_llm_call` for automatic skill routing; `post_tool_call` for effort reconsideration | On after install |
| Middleware (1) | `llm_request` for per-request reasoning effort | On after install |

A skill suggestion is not a correctness guarantee. The automatic hook can load at most one accepted skill **per identified turn**; it can consider many skills and choose again on later turns. The separate multi-skill tool recommends a set but does not load it. Model-routing tools recommend but do not apply a model change. Computer use returns a receipt: an action, a local condition, and completion of the whole goal are separate claims. See [computer-use receipts](docs/DOM-BROWSER-BACKEND.md) for the exact verification rules.

## Automatic skill recommendations

Switchyard checks the active profile's skills before Hermes answers. The install defaults permit a bounded hosted Jev decision for an eligible task. When the decision passes the checks, it can load the selected skill through Hermes for that turn; a later turn can select a different one. Explicit skill instructions, abstention, restricted data, or a loader error can prevent a load. Choose `local_only` and `advisory` above to use local suggestions without a hosted skill-routing call or automatic load. [Routing details](docs/AUTOMATIC-INTEGRATION.md) explain the settings and fail-closed cases.

## Computer use

For public web goals, Switchyard uses a fresh browser profile and chooses from the page's available controls. For desktop apps, it uses Hermes' Cua Driver on Windows, macOS, or Linux. It does not attach to your logged-in browser, handle authentication, or upload files. Jev may choose `DONE`, but Switchyard reports a verified goal only when the required local completion condition also passes. An early local stop remains a completion candidate, not verified success. [Browser and desktop details](docs/DOM-BROWSER-BACKEND.md) cover prerequisites and receipts.

## Toolsets and session exposure

Hermes exposes a plugin tool only when its toolset is selected. Switchyard registers its seven tools under two toolsets:

| Toolset | Tools |
| --- | --- |
| `computer_use` | `jev_computer_use` |
| `hermes_switchyard` | `jev_assess`, `jev_skill_select`, `jev_skill_select_many`, `jev_model_route`, `jev_model_route_approved`, `jev_session_search_rerank` |

An explicit `-t` pin replaces the default selection. Pinning only `computer_use` leaves out the six decision tools; pinning only `hermes_switchyard` leaves out computer use. A pin that names neither toolset exposes none of the seven tools. To expose all seven in one session:

```text
hermes -t computer_use,hermes_switchyard chat
```

Registration does not guarantee that a session can call a tool. `hermes switchyard status --json` checks both for a fresh session. See [toolset setup](docs/SETUP.md#confirm-what-a-session-exposes) for disabled toolsets and explicit pins.

## Configuration

Settings are profile-scoped under `plugins.entries.hermes-switchyard.settings`. The [setup guide](docs/SETUP.md) covers keys, provider routes, opt-down settings, and plugin checks. The [adaptive-effort guide](docs/ADAPTIVE-REASONING-EFFORT.md) explains the effort levels and request behavior.

## Documentation

- [Setup and troubleshooting](docs/SETUP.md)
- [Automatic skill routing](docs/AUTOMATIC-INTEGRATION.md)
- [Adaptive reasoning effort](docs/ADAPTIVE-REASONING-EFFORT.md)
- [Computer-use backend and receipts](docs/DOM-BROWSER-BACKEND.md)
- [Model-routing policy](docs/MODEL-ROUTING.md)
- [Benchmarks and limitations](docs/BENCHMARKS.md)
- [Test coverage](docs/TEST-MATRIX.md) and [release process](docs/RELEASE.md)
- [Changelog](CHANGELOG.md), [contributing](CONTRIBUTING.md), and [security reporting](SECURITY.md)

The project's own code is MIT-licensed. See [third-party references](THIRD_PARTY.md) for upstream material.
