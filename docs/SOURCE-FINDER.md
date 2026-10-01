# Automatic semantic evidence finder

Ask Hermes normally: "In README.md, find the setting that keeps skill routing local."
When enabled, Hermes can choose `switchyard_find` to locate one exact source passage.
No slash command is required. For explicit requests such as "In notes.md, find the retry limit" or "Find the retry limit in notes.md",
the plugin can prefetch source evidence before the first main-model request. Other wording and
unknown filenames retain normal host tool selection. Set `evidence_finder_prefetch: false` to
use only the tool path. The parent feature remains off by default.

## Enable the pilot

Set these plugin settings, then start a fresh Hermes session:

```yaml
evidence_finder_enabled: true
evidence_finder_root: /absolute/path/to/public-source
```

Use an operator-approved directory containing public or sanitized text. The setting
does not authorize other data or override a refused `public_or_sanitized_data_ack`.
The `hermes_switchyard` toolset must be selected for the tool path. The tool is registered but hidden
by default; a disabled optional tool does not make Switchyard status unhealthy.

Hermes receives concise guidance to use the finder for location questions in a known
file, then cite the returned lines. If the path is unknown, normal file search comes
first. On `defer`, Hermes uses normal search/read tools instead of repeating the call.
No fallback main-model request is made inside the tool itself.

Prefetch requires explicit session, task, turn, foreground, and interactive-platform identity.
Missing information skips it. It never reads conversation history or reuses an earlier source
result. Repeated hook invocations for the same scope skip work without reinjecting evidence;
a new scope or query makes a fresh lookup. Host egress envelopes are not interpreted as
permission to upload extra file text, so any supplied envelope skips this prefetch path.
Privacy or network-constraint cues such as "offline", "locally", or "without an external service" skip hosted source lookup. This deliberately conservative eligibility check can also skip benign mentions; it is not a general intent parser or permission grant.
Only exact positive evidence is offered for direct answering. Negative/uncertain results
instruct Hermes to use normal file tools. Evidence stays in current-turn user context, never
in the system prompt. The source SHA and line range identify the captured read, not a future
promise that the file will remain unchanged.

## Contract and limits

- One explicit file beneath the configured root, maximum 80,000 bytes, strict UTF-8.
- At most 240 passages, 24 lines and 2,400 characters per passage. No silent truncation.
- Relative paths only; no hidden components, traversal, symlinks, or nonregular files.
- Descriptor-relative no-follow reads are required. Linux/macOS support this pilot;
  unsupported filesystems/platforms return `defer` and Hermes keeps normal tools.
- A single logical Jev decision combines passage choice with answer-existence scoring,
  within a three-second operation deadline. Existing bounded transport retry rules apply.
- Source text and query pass the Hermes egress scrubber. Unavailable scrubbing or any
  required masking causes local deferral. The scrubber is not a complete data classifier.
- Before returning evidence, reopen the file and compare bytes, identity, and metadata.
  Changed or replaced sources defer, even when a model returned a confident choice.
- Evidence is an exact local substring with original line endings, source path, line
  range, and SHA-256. Confidence is an uncalibrated policy score, not proof of truth.
- `not_found` is limited to this file. It never establishes repository-wide absence.
- Provider failures, malformed responses, contradictory scores, or low confidence defer.
- No result cache, repository crawling, generated quotations, or automatic file edits.

The configured root is operator-owned. Source text is untrusted data, including any
instructions embedded in the selected passage. A final Hermes answer can still err;
the tool guarantees source provenance, not semantic correctness.

## Measurement boundary

The earlier research prototype measured a lookup operation, including a separately
orchestrated Hermes fallback. It did not measure automatic natural-language tool
selection or final answer generation. Those earlier latency figures must not be
presented as this feature's conversation latency.

The PR evaluation compares full normal-prompt conversations with disabled and current
release controls. Acceptance and raw/derived evidence are recorded separately. A
correctness tie does not satisfy issue #139's strict outcome-improvement requirement.
Subscription-included billing is not zero resource use; unknown costs remain unknown.
