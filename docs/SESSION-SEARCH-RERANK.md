# Session search re-rank (`jev_session_search_rerank`)

**In short:** when you ask Hermes something like *"what did we decide about the database migration last week?"*, it searches past sessions by keyword. Keyword search often puts the wrong session first. This tool takes Hermes' search results and asks Jev which one actually answers the question. If Jev isn't confident, the original order stands.

## How the agent uses it

1. Call Hermes' normal `session_search`, with a slightly higher result limit and compact (or adaptive) detail.
2. Pass the ordered results, plus the recall question, to `jev_session_search_rerank`.
3. Use the returned `selected_session_id`, and the optional `match_message_id`, for follow-up reads.

The plugin never runs Hermes' search itself. It treats the input order as the original keyword (full-text search) order.

## Inputs

| Field | Role |
| --- | --- |
| `query` | The recall question |
| `candidates[]` | Compact cards: `session_id`, optional `title` / `snippet`, optional `match_message_ids` |
| Thresholds | `choice_confidence_threshold`, `winning_probability_threshold` (uncalibrated local policy, default 0.8 each) |
| `max_card_chars` | Characters per card after redaction (default 360) |
| `pick_match_message` | Optional second question that picks the best message within the chosen session, from `match_anchors` with previews |
| Bounds | At most 32 cards; at most 720 characters per card; an overall request size budget is enforced |

The defaults come from the `session_search_rerank_*` settings in the [Configuration reference](CONFIGURATION.md#session-search-re-rank).

## Guarantees

- **Redaction:** emails, phone numbers, and common token and secret patterns are redacted before anything is sent.
- **No transcripts:** full transcripts are never sent by default, only short card previews.
- **Safe fallback ("fail-open"):** a provider failure, invalid response, or below-threshold confidence returns the **first** original search result, with `status: fail_open` and a `fail_open_reason`.
- **Empty input:** an empty shortlist returns `status: empty` with no provider call.
- **Refusals:** refusing `public_or_sanitized_data_ack` is an error, not a fail-open.

## Receipt fields

`status`, `selected_session_id`, `match_message_id`, `confidence`, `winning_probability`, `fail_open_reason`, `shortlist_size`, `fts_order_preserved`, `latency_ms` / `total_latency_ms`, `request_count`, `model`, `request_id`, `usage`, `thresholds`, `redaction`.
