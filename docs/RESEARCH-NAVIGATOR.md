# Jev Research Navigator (F1)

The Research Navigator links named claims to public source windows that you already retrieved. It is **off by default**.

## What it does

1. You retrieve public pages with the usual Hermes tools.
2. You call `jev_research_navigator` with a goal, up to 4 claims, and up to 6 original excerpt windows. Each window has an ID, a public `https` URL, and at most 1,200 characters of original text.
3. The tool sends one Jev request. For each claim and window, Jev answers two Noul questions: does the window support the claim, and does it contradict the claim.
4. The tool returns one card per claim.

| Class | Meaning | Band |
| --- | --- | --- |
| `supported` | At least one window has support Noul ≥ 0.85, and no window contradicts. | `act` |
| `contradicted` | At least one window has contradiction Noul ≥ 0.85, and no window supports. | `act` |
| `mixed` | Some windows support and some contradict. Both sides are shown. The tool does not choose. | `ask` |
| `unresolved` | No window passes a threshold, a check failed, or Jev did not answer. | `abstain` |

A high Noul is an assessment for this tool, not proof that a source is true.

## What it does not do

- It does not fetch pages. The URL is display provenance only.
- It does not prove that a window matches its live URL. `source_readback_verified` and `verified` are always `false`.
- It does not write claims, quotes, offsets, or a next search query.
- It does not decide what to cite. You or the coordinator decide.

## Checks done in code before any Jev call

| Check | Result when it fails |
| --- | --- |
| `research_navigator_enabled` is `true` | `skipped`, `feature_disabled` |
| `public_or_sanitized_data_ack` is not refused | `error`, `ack_required` |
| Request shape and limits (4 claims, 6 windows, 1,200 characters per window, 400 goal, 300 claim) | `error`, `invalid_request` |
| At least one claim and one window | `incomplete`, `no_claims` or `no_windows` |
| Each URL is a public `https` URL | `skipped`, `non_public_url` |
| No control characters | `skipped`, `control_characters` |
| Total text at most 12,000 characters | `skipped`, `state_too_large` |
| No restricted document marking (the same rule as adaptive effort, for example CUI, FOUO, proprietary, company confidential) | `skipped`, `restricted_marking` |
| The Hermes egress redactor is present and changes nothing (a public source has no credentials) | `skipped`, `redaction_unavailable` or `credential_detected` |
| `exact_quote`, when given, is a verbatim substring of that same window | pair `quote_absent`; the pair is not sent |
| `sha256`, when given, matches the window text | pair `unassessed`, result `incomplete`, `source_changed`; the window is not sent |
| At most 48 questions and the serialized request is under the 96 KB cap | `skipped`, `request_too_large` |

When a check fails, the tool reads no credential, builds no client, and makes no Jev call. Emails and phone numbers are not refused.

A window with no eligible pair is not sent to Jev and can never be selected.

## Failure behavior

If Jev is unavailable, late, or returns a malformed answer, the result is `status: unavailable`. All windows come back in the original order. No claim is `supported` or `contradicted`. Every eligible pair is `unassessed`. A provider failure is never treated as a semantic "no". There is no fallback provider and no model change.

## Receipt

Each result has a `receipt` with: `feature`, `spec_version`, `policy_version` (`research-v1`), plugin version and source SHA, requested and returned model, window IDs with SHA-256 hashes and URLs without query or fragment, claim IDs, exact-quote flags, pair status and Noul values, claim class and band, selected window IDs, unassessed and skipped pair IDs, `source_readback_verified=false`, `verified=false`, logical batch count, physical attempts and transport retries, deadline and elapsed time, usage, and cost. When the cost is not known, `cost` is `null` and `unknown_cost_count` counts it. When a request may have left but no response proves it, `physical_attempts` is `null`.

The receipt does not hold the goal, claim text, window text, or provider error text.

## Settings

Under `plugins.entries.hermes-switchyard.settings`:

| Key | Default | Meaning |
| --- | --- | --- |
| `research_navigator_enabled` | `false` | Turn the tool on. |
| `research_navigator_deadline_seconds` | `6.0` | Wall-clock budget for the one Jev request. Bounded to the operation deadline. |
| `research_support_threshold` | `0.85` | Support Noul threshold. Bounded to 0.51–1.0. |
| `research_contradiction_threshold` | `0.85` | Contradiction Noul threshold. Bounded to 0.51–1.0. |

## Example request

```json
{
  "goal": "Compare public release support claims",
  "claims": [
    {"id": "r1", "text": "Release A supports Linux"},
    {"id": "r2", "text": "Release A requires a paid plan", "exact_quote": "paid plan required"}
  ],
  "windows": [
    {"id": "w1", "url": "https://example.org/releases/a", "text": "Release A supports Linux. A paid plan required on enterprise devices."},
    {"id": "w2", "url": "https://example.org/faq", "text": "Release A is free for personal use."}
  ]
}
```

The `r2/w2` pair is not sent because the quote is not in `w2`.

## Known limits

- The restricted-marking rule is shared with adaptive effort. A public page that uses a marking word (for example "proprietary" in a license statement) is refused locally.
- Evaluation: see `evaluation/research_navigator/README.md`.
