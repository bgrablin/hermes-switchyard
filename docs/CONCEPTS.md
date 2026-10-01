# Concepts: a plain-language primer

New to Hermes, Jev, or plugins? Start here. This page explains the handful of ideas you need to follow the rest of the Switchyard documentation. It skips internals; each section links to the detailed guide.

## The cast

### Hermes Agent: the assistant you talk to

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is an open-source AI agent from Nous Research. You chat with it in a terminal (`hermes chat`), and it can also run behind a messaging gateway. Behind the scenes, Hermes sends your conversation to a large language model that you choose. This document calls that model the **main model**. Hermes then runs tools on the main model's behalf: it reads files, runs commands, browses, and so on.

### Jev: a fast decision-maker

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is a model from TypeSafe built for quick, structured decisions rather than long answers. You don't ask Jev to write an essay. You ask it a small, closed question:

- **Choice:** "Which one of these options fits best?"
- **Score:** "Where does this fall on this scale?"
- **Noul:** "Is this statement true: yes or no?"

Jev answers in a few hundred milliseconds and reports how confident it is. That makes it a good fit for small calls you want made constantly without slowing the conversation: which skill to load, how hard to think, which button to click.

You reach Jev through one of two providers, **TypeSafe** (direct) or **OpenRouter**, using your own API key. Jev requests are billed by that provider. A ChatGPT, Codex, or other subscription does not cover them.

### Switchyard: the plugin that connects them

A **plugin** is an add-on package that Hermes loads at startup. Plugins can add tools, react to events during a conversation (through **hooks**), and adjust requests before they go out (through **middleware**).

Hermes Switchyard is a plugin. It lets Hermes ask Jev small questions at the right moments, and code inside the plugin decides what to do with each answer. The name comes from a rail yard: Switchyard sets the switches, and Hermes still drives the train.

## Ideas you will see everywhere

### Skills

A **skill** is a packaged set of instructions, a `SKILL.md` file plus optional extras, that teaches Hermes how to handle one kind of task, such as "manage Docker containers." The more skills you install, the harder it gets to pick the right one. Switchyard can pick and load one for you on each turn. See [automatic skill routing](AUTOMATIC-SETUP.md).

### Reasoning effort

Many models accept a **reasoning effort** level such as `low`, `medium`, or `high`. Higher effort means more thinking, which costs more tokens and takes longer. In Hermes you set it with `/reasoning`. Switchyard treats your setting as a **cap**. It can lower effort for easy turns ("thanks!"), but it never goes above your cap unless you opt in. See [adaptive reasoning effort](ADAPTIVE-REASONING-EFFORT.md).

### Tools and toolsets

A **tool** is a function the main model can call, such as `jev_skill_select`. Hermes groups tools into **toolsets**, and a session can only call tools whose toolset is turned on for that session. Switchyard registers seven tools in two toolsets: `hermes_switchyard` and `computer_use`. "Registered" (Hermes knows about the tool) and "callable" (this session can use it) are different things. `hermes switchyard status` checks both. See [Confirm what a session exposes](SETUP.md#confirm-what-a-session-exposes).

### Turns, sessions, and profiles

- A **turn** is one message from you plus everything Hermes does to answer it.
- A **session** is one conversation. Plugin and setting changes take effect only in a **fresh** session, so restart Hermes after installing, updating, or changing settings.
- A **profile** is a separate Hermes home with its own settings, keys, and skills. Switchyard settings and keys are per-profile.

### Receipts

A **receipt** is a small local record of what Switchyard decided and why: for example, "lowered effort from high to low in 180 ms" or "selected skill `docker-management`." Receipts tell you what the plugin did. They don't tell you whether the final answer was right. See [routing receipts](AUTOMATIC-INTEGRATION.md#routing-receipts-and-diagnostics).

### Hosted vs. local decisions

- A **local** decision happens entirely on your machine with simple rules, such as recognizing "hi" as a greeting. It costs nothing and sends nothing.
- A **hosted** decision sends a small, bounded, scrubbed piece of the current request to Jev. It costs a fraction of a cent and takes a few hundred milliseconds.

Switchyard uses local decisions where it safely can and hosted ones where they help. You can force local-only behavior. See [Privacy at a glance](../README.md#privacy-at-a-glance).

### "Fail closed"

You will see this phrase a lot. It means that when something goes wrong (Jev is slow, the answer is malformed, a privacy check fails, or a key is missing), Switchyard does the safe thing: it keeps your original setting, skips the hosted call, or stops. It never guesses. A failure never quietly turns into a riskier action.

### "Advisory" vs. "applied"

Some features only make a recommendation. For example, `jev_model_route` can suggest a model, but it never switches your model (`applied: false`). Other features apply their decision: automatic skill routing loads the skill, and adaptive effort changes the effort level. Each guide states which kind it is.

### Public or sanitized data

Hosted Jev calls go to an external provider. For its **automatic** features, Switchyard scrubs recognizable secrets and keeps clearly marked confidential text local. That scrubbing is a safety net, not a guarantee. **Explicit tools** such as `jev_assess` send what the caller passes, after only an acknowledgement check, so their inputs must already be public or sanitized. The setting `public_or_sanitized_data_ack` records your agreement that what you send is public or already cleaned up. It does not make private data safe to send. For details, read the [privacy section](../README.md#privacy-at-a-glance).

## Where to go next

- **Install it:** [Quickstart](../README.md#quickstart)
- **Find a specific guide:** [Documentation map](README.md)
- **Change a setting:** [Configuration reference](CONFIGURATION.md)
