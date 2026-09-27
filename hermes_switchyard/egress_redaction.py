"""Redact user text before it goes to Jev.

Switchyard does not keep its own secret-pattern list. It uses the Hermes egress
scrubber (``agent.redact.redact_for_egress``), which masks known secret shapes
and fails closed. When that scrubber is not importable (older Hermes), callers
get ``None`` and send only non-text metadata.

The Hermes scrubber targets credentials. Switchyard adds two masks for shapes
it does not cover: lowercase ``password=VALUE`` / ``--password VALUE`` forms and
Luhn-valid payment card numbers. Masking never blocks, so a false match costs
one hidden word and the decision still runs. All patterns are bounded.
"""

from __future__ import annotations

import re
from collections.abc import Callable

REDACTION_UNAVAILABLE_REASON = "redaction_unavailable"
_HERMES_UNAVAILABLE_MARKER = "[redaction-unavailable]"

_redactor: Callable[[str], str] | None = None
_redactor_loaded = False


def _load_redactor() -> Callable[[str], str] | None:
    global _redactor, _redactor_loaded
    if not _redactor_loaded:
        try:
            from agent.redact import redact_for_egress  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 -- any import failure means no redactor
            redact_for_egress = None
        _redactor = redact_for_egress if callable(redact_for_egress) else None
        _redactor_loaded = True
    return _redactor


# Bounded: 13-19 digits with optional single space/dash separators. Linear time.
_CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")


_SECRET_WORD = r"(?:pass(?:word|wd|phrase)|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)"
_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_.-]{0,40}" + _SECRET_WORD + r"[\"']?\]?\s{0,3}[:=]\s{0,3}[\"']?)"
    r"(?![<$%{*])([^\s\"'`,;()\[\]{}]{1,256})"
)
_FLAG_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9-])(--?[A-Za-z0-9_-]{0,40}" + _SECRET_WORD + r"[= ]\s{0,3}[\"']?)"
    r"(?![<$%{*-])([^\s\"'`,;()\[\]{}]{1,256})"
)


def _mask_value(match: re.Match[str]) -> str:
    value = match.group(2)
    if value == "***" or value.isdigit() or value.lower() in {"true", "false", "none", "null"}:
        return match.group(0)
    return match.group(1) + "***"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[card]" if _luhn_ok(digits) else match.group(0)


def mask_personal_data(text: str) -> str:
    """Mask secret assignments and flags Hermes misses, and Luhn-valid card numbers."""
    text = _ASSIGN_RE.sub(_mask_value, text)
    text = _FLAG_RE.sub(_mask_value, text)
    return _CARD_RE.sub(_mask_card, text)


def redaction_available() -> bool:
    return _load_redactor() is not None


def redact_for_jev(text: str) -> tuple[str | None, str | None]:
    """Return ``(redacted_text, None)`` or ``(None, reason)`` when text must not be sent."""
    redactor = _load_redactor()
    if redactor is None:
        return None, REDACTION_UNAVAILABLE_REASON
    try:
        redacted = redactor(text)
    except Exception:  # noqa: BLE001 -- fail closed
        return None, REDACTION_UNAVAILABLE_REASON
    if not isinstance(redacted, str) or redacted == _HERMES_UNAVAILABLE_MARKER:
        return None, REDACTION_UNAVAILABLE_REASON
    return mask_personal_data(redacted), None


def _reset_for_tests(redactor: Callable[[str], str] | None = None, *, loaded: bool = False) -> None:
    global _redactor, _redactor_loaded
    _redactor = redactor
    _redactor_loaded = loaded
