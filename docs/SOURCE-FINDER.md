# Automatic source prefetch

Ask Hermes normally: "In notes.md, find the retry limit."
When enabled, a pre-turn hook can retrieve an exact supporting passage before the
first main-model request. There is no slash command or callable finder tool.
Unsupported wording, unknown filenames, recognized compound requests, and declined lookups
continue through ordinary Hermes search/read tools without finder discovery.

## Enable the pilot

Set these plugin settings and start a fresh Hermes session:

```yaml
evidence_finder_enabled: true
evidence_finder_root: /absolute/path/to/public-source
```

The directory must contain operator-approved public or sanitized text. File names
in requests are relative to this configured source root. The feature stays off by
default while qualification continues. It requires the existing pre_llm_call hook;
no toolset selection, execution middleware, or Hermes source modification is needed.
A refused public_or_sanitized_data_ack disables registration. The earlier draft's
evidence_finder_prefetch switch and switchyard_find tool have been removed.

## Eligibility and fallback

The original user message must name one file in a supported form: "In file.md, find
..." or "Find ... in file.md". Quoted paths are accepted. A second line may request
a complete supported JSON format; other multiline and recognized compound work stays with
Hermes. Explicit printable, nonblank session, task, and turn identities, a foreground
parent identity, and a supported interactive platform are required. Missing or
malformed identity skips prefetch before source I/O or provider work.

Privacy and network constraints in the original request skip lookup. Any supplied
host egress envelope also skips lookup because it may authorize a smaller payload.
The privacy and compound-action cue scanners are conservative heuristics. They can skip benign mentions, do not recognize every possible natural-language constraint or second action, and are not
a general intent parser or data-loss-prevention system. The hook never reads history
or model-generated tool arguments. Repeated callbacks for the same scope and message
skip work without reinjecting previous evidence. Host-envelope refusals are retained separately from ordinary duplicate suppression and never evicted to admit another lookup. A later duplicate cannot broaden a refusal by omitting the envelope. If the bounded refusal store fills, prefetch stops for that hook instance until the plugin is reloaded; ordinary Hermes tools continue normally. A new scope or query reads afresh.

Only exact positive evidence is supplied for direct answering. Missing, uncertain,
invalid, or late results keep normal file tools. There is no internal main-model
fallback call. Source data is untrusted current-turn user context, never a system
instruction. A final Hermes answer can still be wrong; provenance is not correctness.

## Contract and limits

- One relative file beneath the configured absolute root; maximum 80,000 bytes,
  strict UTF-8, no hidden components, traversal, symlinks, or nonregular files.
- Descriptor-relative no-follow reads are required. Unsupported hosts defer.
- At most 240 passages, 24 lines and 2,400 characters per passage; no silent truncation.
- One logical Jev request combines passage selection and existence scoring. Its
  three-second acceptance deadline includes verification and cleanup. Synchronous
  filesystem calls cannot be forcibly interrupted. Bounded transport retry rules apply.
- Source and query pass Hermes's egress scrubber. Missing scrubbing or required masking
  defers before network work; a clean result does not classify all private data.
- A second read verifies the same bytes, identity, and metadata after inference.
  Changed/replaced sources defer. Returned evidence preserves original line endings,
  source-relative path, line range, and SHA-256 of the captured read.
- No result cache, repository crawling, generated quotations, or automatic edits.

## Evaluation

Evaluate complete ordinary-prompt conversations, including selection, Jev calls,
verification, fallback, and final answers. Operation-only timing is not conversation
latency. Retain controls, failures, unknown billing, source revisions, and scope checks.
The previous callable-tool prototype and the narrowed prefetch pilot are separate
candidates. Historical results keep their original acceptance verdicts.

Future evaluations declare either efficiency (preserved outcomes with material time
savings) or capability (better outcomes within a bounded latency budget) before calls.
See [the prospective evaluation policy](VALUE-EVALUATION.md).
