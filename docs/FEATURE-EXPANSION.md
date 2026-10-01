# Tool review, retrieval screening, and conservative output handling

These are development candidates for 0.6.0, not a new release. Model switching
remains advisory. No Hermes core patch or private execution override is required. The tested
upstream pin includes plugin-guard v8; older install scanners can reject the
inert adversarial fixtures in this repository.

## What changes

| Feature | Behavior | Default |
| --- | --- | --- |
| Retrieved-text screen | Locally withholds session-search cards with explicit instruction-override or role-spoof indicators, including provider-failure fallback. Stops the DOM loop on the same indicators in its observations. | Enforcement off; search indicators shadow-only |
| Catalog review | `hermes switchyard scan-catalog PATH` reads a local package or MCP config directory and reports content hashes, rule locations, and coverage gaps. | Explicit command only |
| Consequential tool gate | Requests native human approval on code-defined credential, deletion, or irreversible-operation indicators. | Off |
| Smart approval provider | Six typed Jev questions feed a deterministic APPROVE/DENY/ESCALATE decision for native `auxiliary.approval`. | Off; separate provider selection required |
| Repeated output compaction | Replaces consecutive identical successful-terminal-output lines with one line and an explicit repetition count. | Off |
| Cross-tool stuck advice | After three errors across at least two tools, asks once whether they share an unresolved prerequisite. A strong answer adds advice. | Off |
| Browser plan cache | Reuses successful public link decisions for repeat goals when page evidence and route scope match. | Off |

Enforcement needs explicit `retrieved_screen_enabled: true`. By default, search
receipts report shadow indicators without withholding cards, and DOM runs keep
the existing behavior. The screen recognizes a bounded set of warning patterns.
DOM coverage includes page/link URL components, with up to three local percent/form
decoding passes. URLs and matched text are not added to screening receipts. It does not establish
that content is trustworthy, scan unobserved page text, or replace the host's
instruction boundary. Search receipts contain warning codes and withheld counts,
not matched text. Benign documents demonstrating an attack may be withheld.

## Enable individual candidates

Settings live under `plugins.entries.hermes-switchyard.settings`:

```yaml
retrieved_screen_enabled: true
consequential_tool_gate: true
repeated_output_compaction: true
cross_tool_stuck_detection: true
browser_plan_cache: true
```

Start a fresh session after changing settings. These switches are independent.
The two new native tool hooks have no registered listeners when disabled.

`pre_tool_call` covers `terminal`, `execute_code`, `write_file`, `patch`, and
`delegate_task`. Hermes' `approve` directive means **request human approval**;
it never means this plugin approved execution. Persistent approval keys bind the
tool and entire bounded input; incomplete inspection uses an invocation-only key.
Native permissions still apply.
Hermes may ignore a failed or timed-out plugin hook, so this additive check is
not a complete authorization boundary.

Cross-tool advice needs nonempty session, task, turn, and tool-call IDs. Unknown
scope abstains. It keeps at most 128 turns for ten minutes and 64 call IDs per
turn. After three consecutive errors, up to 500 redacted characters per error
and generic tool categories go to Jev, with a 0.8-second deadline. Success,
uninspectable output, and independent failures do not justify a nudge. Missing
credentials, malformed answers, redaction failure, and timeout preserve output.
It neither blocks tools nor forces another agent iteration. Hermes already owns
exact-loop guards and bounded verification continuation; this candidate adds
only cross-tool obstacle advice.

## Native smart approvals

Enable the provider and explicitly select it:

```yaml
plugins:
  entries:
    hermes-switchyard:
      settings:
        smart_approval_provider: true
        public_or_sanitized_data_ack: true
auxiliary:
  approval:
    provider: switchyard-approvals
    model: typesafe/jev-1.13-20260917
    timeout: 2
approvals:
  mode: smart
```

This route uses the existing `OPENROUTER_API_KEY` secret and fixed Decisions
endpoint. It accepts native smart-review envelopes only. It does not select the
main conversation model, rewrite another provider, or execute the reviewed text.
Known credential and irreversible-operation patterns escalate before any hosted
request. Commands or operator policies that change under redaction, oversized
input, missing consent, uncertain signals, invalid answers, and provider failures
also escalate. There is no text-generation fallback inside the reviewer.

One request asks independent questions about the verdict, sufficient evidence of
safety, secret access, outbound transfer, irreversible effects, and manipulation
of the review. Confidence/probability thresholds are conservative policy choices,
not calibrated safety guarantees. This provider can require more human review
than the native reviewer. A 16-case development holdout approved only 3 of 7
clearly safe commands and approved none of the 9 unsafe/uncertain cases. This
did not qualify it for default use; the provider remains experimental. Keep it
opt-in until a workload-specific evaluation supports promotion. To restore native review, restore the previous
`auxiliary.approval` provider/model and disable `smart_approval_provider`.

## Output preservation and compaction

The pruning switch only handles a successful terminal JSON result with integer
exit code zero, one output field, no explicit stderr/error, and no truncation
indicator. It processes 4,000–256,000 characters and compresses only runs of at
least four identical complete lines. Unique lines, other JSON fields, failures,
and repetition counts remain available. This changes the model-facing result;
consumers expecting the exact original stdout should leave it off.

Jev does not choose which evidence to keep, summarize, or drop. Switchyard does
not replace the native `ContextCompressor` or change its mandatory threshold,
summary, tail protection, or session-search recovery. The timing probe in
`evaluation/feature_expansion` asks `new_topic` and `refers_back` in shadow only.
Correct topic classification alone does not establish retained recall, lower
end-to-end latency, or a better compaction policy. Native summary/recovery remains
the baseline for any future long-session evaluation.

## Browser cache boundaries

Plans are memory-only, capped at 32 entries with a five-minute lifetime. Keys
bind the goal, starting observation, completion predicate, minimum actions, and
live provider/model/profile/credential scope. Each step also matches current
observation and question evidence. A mismatch abandons the remaining cached
plan. Session-specific document handles and offscreen document height are not
cross-session content identity; the normal action loop still checks a fresh
handle, offered target, URL, and destination policy immediately before acting.

Only same-origin HTTPS links without query strings can seed a successful plan.
Typing, buttons, scrolls, waits, cross-origin actions, partial runs, and failed
outcomes do not seed it. It never caches DONE or certifies a goal. A locally
satisfied predicate remains a completion candidate for the coordinator. Receipts
separate `plan_cache_hits` from actual `jev_request_count` and attempted requests.
Dynamic page changes can eliminate cache hits; a hit is not promised.

## Catalog admission review

Run this on an unpacked candidate **before** installing it:

```text
hermes switchyard scan-catalog ./candidate-package
```

The command makes no network requests and executes no package code. It checks
bounded UTF-8 source/configuration files for download-and-execute patterns,
dynamic execution, credential/network access, instruction-override indicators,
and common MCP launcher/endpoint hazards. Command indicators also apply to skill
Markdown and configuration text; benign command examples can need review. Findings are review indicators, not a
malware verdict. A human still decides admission; the command does not install,
whitelist, approve, or override Hermes' existing install scanner.

The report includes per-file SHA-256, a manifest digest, rule IDs and locations,
and skipped coverage. Limits: 512 files, 1,024 directory entries, 256 KB per file,
4 MB total, 32 directory levels, and 4,096 findings. Symlinks, excluded
directories, unsupported types, changed/unreadable files, and exhausted budgets are explicit gaps. File reads
use descriptor-relative no-follow operations; platforms lacking those primitives
return `safe_scan_unavailable` instead of scanning unsafely. Windows currently
falls into this case. Exit zero requires complete supported-text coverage and
no indicators; every other outcome exits one. Rescan the exact bytes admitted.
