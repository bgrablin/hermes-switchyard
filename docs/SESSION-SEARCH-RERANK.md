# Jev session_search re-rank

## Problem

Hermes stock `session_search` is FTS/lexical. Recall-style questions often surface the wrong session first.

## Agent flow

1. Call stock Hermes `session_search` with a slightly higher limit and compact (or adaptive) detail.
2. Pass the ordered shortlist plus the user's recall question to Switchyard's `jev_session_search_rerank`.
3. Use the returned `selected_session_id` (and optional `match_message_id`) for follow-up reads.

The plugin does not invoke Hermes FTS itself. Input order is treated as stock FTS order.

## Tool: `jev_session_search_rerank`

| Field | Role |
| --- | --- |
| `query` | Recall question |
| `candidates[]` | Compact cards: `session_id`, optional `title` / `snippet`, optional `match_message_ids` |
| Thresholds | `choice_confidence_threshold`, `winning_probability_threshold` (uncalibrated local policy) |
| `max_card_chars` | Per-card cap after redaction (default 480) |
| `pick_match_message` | Optional second Choice among message-id anchors |

### Guarantees

- Emails, phones, and common token/secret patterns are redacted before egress.
- Full transcripts are never sent by default — only capped card previews.
- **Fail-open:** provider failure, invalid response, or below-threshold confidence / winning probability returns the **first FTS candidate** with `status: fail_open` and `fail_open_reason`.
- Empty shortlist returns `status: empty` with no provider call.
- Refusing `public_or_sanitized_data_ack` still raises (not fail-open).

### Receipt fields

`status`, `selected_session_id`, `match_message_id`, `confidence`, `winning_probability`, `fail_open_reason`, `shortlist_size`, `fts_order_preserved`, `latency_ms` / `total_latency_ms`, `request_count`, `model`, `request_id`, `usage`, `thresholds`, `redaction`.
