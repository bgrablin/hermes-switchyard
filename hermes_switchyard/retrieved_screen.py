"""Bounded local warning screen for instructions embedded in retrieved text.

This is defense in depth, not a trust classifier or an authorization decision.
No text, matched span, URL, or credential is included in its receipts.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus

MAX_TEXT = 96_000
_PATTERNS = (
    (
        "instruction_override",
        re.compile(
            r"(?im)^\s*(?:attention[,:]?\s*)?(?:ignore|disregard|override)\s+"
            r"(?:all\s+)?(?:previous|prior|earlier|system|developer|safety)\s+"
            r"(?:instructions?|prompts?|rules?|messages?|polic(?:y|ies))\b"
        ),
    ),
    (
        "role_spoof",
        re.compile(
            r"(?im)^\s*(?:<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)|"
            r"<(?:system|developer)>|\[(?:system|developer)\]\s*(?:ignore|you|override)|"
            r"(?:system|developer)\s*:\s*(?:ignore|disregard|override|you must))"
        ),
    ),
    (
        "agent_directive",
        re.compile(
            r"(?i)\b(?:assistant|chatgpt|AI agent|language model)[,:!]\s*"
            r"(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|prior|system|developer)\b"
        ),
    ),
)


def screen_text(text: Any) -> tuple[str, ...]:
    """Return closed-set warnings; examine full bounded text before truncation."""
    if not isinstance(text, str):
        return ()
    if len(text) > MAX_TEXT:
        return ("screen_limit",)
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Cf")
    return tuple(name for name, pattern in _PATTERNS if pattern.search(normalized))


def card_anchors(card: Mapping[str, Any]) -> Any:
    """Shared alias precedence for screening and candidate normalization."""
    for key in ("match_anchors", "match_message_ids", "match_message_id"):
        value = card.get(key)
        if value is not None:
            return value
    return None


def anchor_preview(anchor: Mapping[str, Any]) -> Any:
    """Return the exact raw preview that normalization will consume."""
    return anchor.get("preview") or anchor.get("snippet") or ""


def screen_card(card: Any) -> tuple[str, ...]:
    if not isinstance(card, Mapping):
        return ()  # Existing schema validation owns malformed objects.
    fields = [card.get("title"), card.get("snippet")]
    anchors = card_anchors(card)
    if isinstance(anchors, (list, tuple)):
        if len(anchors) > 32:
            return ("screen_limit",)
        fields.extend(anchor_preview(a) for a in anchors if isinstance(a, Mapping))
    if sum(len(field) for field in fields if isinstance(field, str)) > MAX_TEXT:
        return ("screen_limit",)
    return tuple(sorted({reason for field in fields for reason in screen_text(field)}))


def screen_shortlist(candidates: Any) -> tuple[Any, dict[str, Any]]:
    """Keep clean cards, including on provider failure. Do not mutate input."""
    if not isinstance(candidates, (list, tuple)) or len(candidates) > 32:
        return candidates, {"screened": False, "withheld": 0, "reasons": []}
    kept, reasons = [], set()
    for card in candidates:
        found = screen_card(card)
        if found:
            reasons.update(found)
        else:
            kept.append(card)
    return kept, {
        "screened": True,
        "withheld": len(candidates) - len(kept),
        "reasons": sorted(reasons),
    }


def screen_url(value: Any) -> tuple[str, ...]:
    """Screen URL components locally, including three common decoding layers."""
    if not isinstance(value, str):
        return ()
    if len(value) > MAX_TEXT:
        return ("screen_limit",)
    reasons = set()
    current = value
    for attempt in range(4):
        for part in (current, *re.split(r"[/?&#=]", current)):
            reasons.update(screen_text(part))
        if attempt == 3:
            break
        decoded = unquote_plus(current)
        if decoded == current:
            break
        current = decoded
    return tuple(sorted(reasons))


def screen_page(page: Mapping[str, Any]) -> tuple[str, ...]:
    fields = [page.get("title"), page.get("text")]
    urls = [page.get("url")]
    elements = page.get("elements")
    if isinstance(elements, list):
        if len(elements) > 2000:
            return ("screen_limit",)
        fields.extend(e.get("label") for e in elements if isinstance(e, Mapping))
        urls.extend(e.get("href") for e in elements if isinstance(e, Mapping))
    if sum(len(value) for value in fields + urls if isinstance(value, str)) > MAX_TEXT:
        return ("screen_limit",)
    reasons = {reason for field in fields for reason in screen_text(field)}
    reasons.update(reason for url in urls for reason in screen_url(url))
    return tuple(sorted(reasons))
