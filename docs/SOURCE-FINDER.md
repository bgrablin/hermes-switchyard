# Automatic source prefetch (opt-in pilot)

**In short:** ask Hermes a normal question that names a file, such as *"In notes.md, find the retry limit."* With this pilot turned on, Switchyard finds the exact supporting passage **before** Hermes' first model call, and hands it over with the file name, line range, and a hash of what was read. Hermes can then answer directly instead of spending a round searching and reading.

There's nothing new to learn: no slash command and no tool. If your request doesn't fit the supported pattern, or the evidence is uncertain, Hermes does what it always does and uses its normal search and read tools.

**Status:** off by default. It is a pilot that is still being qualified. New in 0.6.0.

## Turn it on

Set two settings, then start a fresh Hermes session:

```yaml
evidence_finder_enabled: true
evidence_finder_root: /absolute/path/to/public-source
```

Using the command line:

```text
hermes config set plugins.entries.hermes-switchyard.settings.evidence_finder_enabled true
hermes config set plugins.entries.hermes-switchyard.settings.evidence_finder_root /absolute/path/to/public-source
```

- **The folder** must contain text you've approved as public or sanitized, because passages from it are sent to Jev.
- **File names** in your requests are relative to that folder.
- It uses the existing `pre_llm_call` hook. No toolset, middleware, or Hermes change is needed.
- If `public_or_sanitized_data_ack` is `false`, the feature doesn't register.

The earlier draft's `evidence_finder_prefetch` switch and `switchyard_find` tool no longer exist.

## How to ask

**One file:**

- "In file.md, find …"
- "Find … in file.md"

Quoted paths are fine.

**Two to eight files:** list them in backticks:

> In `implementation.md`, `policy.md` and `check.md`, find the queue overload behavior.

**Optional second line:** you may ask for a complete, supported JSON format on a second line. Other multi-line or compound requests ("find X *and then* rewrite it") are left to Hermes.

## When it steps aside

Prefetch skips the turn, and Hermes handles it normally, when:

- the wording or list syntax isn't recognized, or a file is unknown;
- the request is compound, or mentions privacy or network constraints;
- Hermes supplied an egress policy envelope for the turn (it might authorize a smaller payload);
- the turn isn't a foreground interactive one. Prefetch requires explicit, printable session, task, and turn IDs, an empty parent-session ID, and a supported platform. Missing or malformed IDs skip prefetch before any file or network work.
- the evidence is missing, uncertain, invalid, or late.

There's no hidden fallback model call. The cue detectors for privacy and compound requests are conservative heuristics. They can skip harmless requests, they won't catch every constraint, and they are not a general intent parser or DLP system.

The hook never reads conversation history or tool arguments the model wrote.

## Trust and limits

- **Only exact, positive evidence is passed on.** Source text is treated as untrusted user-turn context, never as a system instruction.
- **Provenance is not correctness.** Hermes' final answer can still be wrong.
- **No caching or reuse.** Evidence is never cached or re-injected from an earlier turn.

### Duplicate and refusal tracking

- **Duplicates.** The 256 most recent eligible scope-and-message keys are remembered, and a repeat within that window is skipped. Once a key is evicted, a repeat may run a fresh lookup. Within an unrefused scope, a new question always reads the files afresh.
- **Host refusals.** When a host envelope refuses a session, task, or turn, the refusal covers that whole scope, even if the message changes or was initially malformed. Refusals are stored separately from duplicates and are never evicted to make room.
- **No broadening.** A later callback can't widen a refusal by changing the text or dropping the envelope.
- **In-flight lookups.** A refusal recorded while a lookup is running suppresses its result. It can't recall a provider request that has already been sent.
- **A full refusal store** stops prefetch for that hook instance until the plugin reloads. Ordinary Hermes tools keep working.

## Technical contract

**Reading files**

- A single-file lookup reads one relative file under the configured absolute root, up to 80,000 bytes of strict UTF-8.
- Hidden path components, `..` traversal, symlinks, and non-regular files are refused.
- Reads must be descriptor-relative and no-follow. Hosts that can't do that skip prefetch.
- At most 240 passages, each up to 24 lines and 2,400 characters. Nothing is silently truncated.

**Asking Jev**

- One logical Jev request selects the passage and scores whether the answer exists.
- The 3-second acceptance deadline includes verification and cleanup. Synchronous filesystem calls can't be forcibly interrupted. Bounded transport retry rules apply.
- The source and query both go through Hermes' egress scrubber. If scrubbing is unavailable or masking would be needed, the lookup steps aside before any network work. A clean scrub doesn't mean all private data was caught.

**Verifying the result**

- After Jev answers, the file is read a second time to confirm the same bytes, identity, and metadata. A changed or replaced file means the lookup steps aside.
- Returned evidence keeps the original line endings, the relative path, the line range, and the SHA-256 of the captured read.

**Never done:** result caching, repository crawling, generated quotations, or automatic edits.

### Multiple named files

The same settings also handle an explicit backtick list of two to eight files.

- **One request for all files.** A single bounded Jev request evaluates each passage by a stable key, including supporting configuration, tests, and contradicting documentation.
- **Duplicates and citations.** Exact duplicate text shares citations. Distinct relevant passages stay separate, each with its own file hash and line range.
- **Rechecks.** Every captured file is rechecked after inference, including files scored irrelevant.
- **Limits.** Input is capped at 80,000 bytes and 64 passages in total. Output is capped at eight distinct passages and 12,000 characters.
- **All or nothing.** Uncertain decisions, missing results, and exceeded budgets hand the *whole* lookup back to Hermes' normal tools. A bundle is never silently truncated, and absence is never claimed for the whole repository.

Limits and relevance thresholds are local policy, not calibrated probabilities. The 3-second deadline, source-root boundary, acknowledgement, scrubbing, and foreground-only rules all still apply. Unrecognized list syntax goes to Hermes' ordinary tools.

## Evaluation

This pilot is judged on **complete conversations** with ordinary prompts: selection, Jev calls, verification, fallback, and the final answer. Timing a single operation doesn't measure conversation latency. Evaluations keep their controls, failures, unknown billing, source revisions, and scope checks.

The earlier callable-tool prototype and this narrowed pilot are separate candidates, and historical results keep their original verdicts. Future evaluations declare up front whether they're testing **efficiency** (same outcomes, materially faster) or **capability** (better outcomes within a latency budget). See the [prospective evaluation policy](VALUE-EVALUATION.md).

The multi-file extension passed an 80-conversation native pilot. Component results and limits are in [the experiment record](AWESOME-JEV-EVALUATION.md).
