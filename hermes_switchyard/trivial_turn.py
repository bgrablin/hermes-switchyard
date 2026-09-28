"""Closed-list trivial-turn detector shared by skill routing and adaptive effort.

A greeting, thanks, or acknowledgement never names a specialist skill and needs no deep
reasoning, so a hosted call only adds latency. The rule is a closed word list, not a length
rule, so short task requests (``fix ci``) are not trivial.

Light-turn predicates (greeting-class instructions, pure read-only listings, short
no-action explanations) skip hosted skill routing when a specialist skill cannot help.
Adaptive effort still uses only ``is_trivial_turn`` / greeting-class for local bypass.
"""
from __future__ import annotations

import re

TRIVIAL_ACK_MAX_WORDS = 6
TRIVIAL_SYMBOL_MAX_CHARS = 8
TRIVIAL_WORD_RE = re.compile(r"[a-z0-9']+")
TRIVIAL_ACK_WORDS = frozenset(
    """
    hi hello hey hiya yo morning afternoon evening night gm gn bye goodbye cheers
    thanks thank thx ty tysm you so very much appreciate appreciated
    ok okay k kk yes yep yeah yup sure no nope nah fine right correct agreed
    great cool nice perfect awesome good excellent lgtm got it sounds that's
    go ahead continue proceed done again
    """.split()
)

# Receipt / hosted-skip reason for non-ack light turns that still need no skill routing.
LIGHT_NO_SKILL_REASON = "light_no_skill"

# Instructional prompts whose only deliverable is a greeting sentence/reply.
_GREETING_ASK_RE = re.compile(
    r"\b(?:reply|respond|say|write|give|send)\b.{0,48}\b(?:greeting|hello|hi)\b"
    r"|\b(?:one|a|short)\s+(?:\w+\s+){0,3}(?:greeting|hello)\b"
    r"|\bgreeting\s+sentence\b",
    re.IGNORECASE | re.DOTALL,
)
_GREETING_CLASS_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"skill|debug|deploy|fix|patch|install|delete|browse|docker|printer|"
    r"error|plan|rollback|compose|maintenance|unreachable|logs?"
    r")\b",
    re.IGNORECASE,
)

# Pure cwd / directory listing with an explicit no-mutation constraint.
_LIST_DIR_RE = re.compile(
    r"\b(?:"
    r"ls\s+-la\b"
    r"|list\s+(?:the\s+)?(?:names?\s+of\s+)?(?:entries|files|directories|contents)\b"
    r"|list\s+(?:the\s+)?(?:cwd|current\s+(?:working\s+)?directory)\b"
    r"|names?\s+of\s+entries\s+in\s+the\s+current\s+working\s+directory\b"
    r")",
    re.IGNORECASE,
)
_NO_MUTATE_RE = re.compile(
    r"\b(?:do\s+not|don't|without)\b.{0,80}\b(?:write|patch|delete|install|change|modify|edit)\b"
    r"|\b(?:safe\s+)?read[- ]only\b",
    re.IGNORECASE | re.DOTALL,
)
_LIST_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"skill|debug|deploy|printer|error\s+logs?|compose\s+health|"
    r"docker\s+update|rollback|unreachable"
    r")\b",
    re.IGNORECASE,
)

# Short explanatory Q&A that forbids tools/commands and does not ask for a skill.
_EXPLAIN_RE = re.compile(
    r"\b(?:"
    r"explain|what\s+(?:is|does|are)|give\s+one\s+example|numbered\s+steps|"
    r"in\s+three\s+short\s+numbered\s+steps"
    r")\b",
    re.IGNORECASE,
)
_NO_ACTION_RE = re.compile(
    r"\b(?:do\s+not|don't)\b.{0,48}\b(?:run|execute|use\s+tools|change|browse|edit|write)\b",
    re.IGNORECASE | re.DOTALL,
)
_EXPLAIN_SKILL_CUE_RE = re.compile(
    r"\b(?:"
    r"use\s+(?:a\s+)?(?:relevant\s+)?(?:available\s+)?skill"
    r"|EVAL_SKILL|skill_view|load\s+the\s+\w[\w-]*\s+skill"
    r")\b",
    re.IGNORECASE,
)
_EXPLAIN_CONSEQUENTIAL_RE = re.compile(
    r"\b(?:"
    r"plan\s+a|prioritize\s+risk|rollback|production|maintenance\s+window|"
    r"home-lab|unreachable|ERROR\s+lines"
    r")\b",
    re.IGNORECASE,
)

_LIGHT_TURN_MAX_CHARS = 700


def is_trivial_turn(text: str) -> bool:
    """True for a greeting, thanks, or acknowledgement with no task words.

    Only words from a closed acknowledgement list qualify, so a short request
    such as ``fix ci`` still goes to the hosted selector. Text with no word
    characters (an emoji or ``?``) qualifies when it is very short.
    """
    stripped = text.strip()
    words = TRIVIAL_WORD_RE.findall(stripped.casefold())
    if not words:
        return 0 < len(stripped) <= TRIVIAL_SYMBOL_MAX_CHARS
    return len(words) <= TRIVIAL_ACK_MAX_WORDS and all(word in TRIVIAL_ACK_WORDS for word in words)


def is_greeting_class_prompt(text: str) -> bool:
    """True when the prompt only asks for a greeting reply (no specialist task).

    Covers instructional battery-style prompts such as ``Reply with exactly one
    short greeting sentence…`` that the closed acknowledgement list alone misses.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _LIGHT_TURN_MAX_CHARS:
        return False
    if is_trivial_turn(stripped):
        return True
    if not _GREETING_ASK_RE.search(stripped):
        return False
    return _GREETING_CLASS_NEGATIVE_RE.search(stripped) is None


def is_readonly_listing_prompt(text: str) -> bool:
    """True for a pure directory-listing request with an explicit no-mutation rule.

    Specialist skills cannot help ``ls -la .`` / list-cwd turns; hosted skill
    selection only adds latency.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _LIGHT_TURN_MAX_CHARS:
        return False
    if not _LIST_DIR_RE.search(stripped):
        return False
    if not _NO_MUTATE_RE.search(stripped):
        return False
    return _LIST_NEGATIVE_RE.search(stripped) is None


def is_light_explanation_prompt(text: str) -> bool:
    """True for a short no-action explanation that does not ask to load a skill.

    Covers light multi-step Q&A (``In three short numbered steps… Do not run
    commands``). Consequential planning and skill-eval prompts stay hosted.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _LIGHT_TURN_MAX_CHARS:
        return False
    if not _EXPLAIN_RE.search(stripped):
        return False
    if not _NO_ACTION_RE.search(stripped):
        return False
    if _EXPLAIN_SKILL_CUE_RE.search(stripped):
        return False
    return _EXPLAIN_CONSEQUENTIAL_RE.search(stripped) is None


def hosted_skill_bypass_reason(text: str) -> str | None:
    """Return a hosted skill-routing bypass reason, or None when hosting may help.

    ``trivial_turn`` covers closed-list acknowledgements. ``light_no_skill`` covers
    greeting-class instructions, pure read-only listings, and short no-action
    explanations where a specialist skill cannot earn its keep.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if is_trivial_turn(stripped):
        return "trivial_turn"
    if is_greeting_class_prompt(stripped):
        return LIGHT_NO_SKILL_REASON
    if is_readonly_listing_prompt(stripped):
        return LIGHT_NO_SKILL_REASON
    if is_light_explanation_prompt(stripped):
        return LIGHT_NO_SKILL_REASON
    return None
