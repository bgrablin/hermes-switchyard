"""Closed-list trivial-turn detector shared by skill routing and adaptive effort.

A greeting, thanks, or acknowledgement never names a specialist skill and needs no deep
reasoning, so a hosted call only adds latency. The rule is a closed word list, not a length
rule, so short task requests (``fix ci``) are not trivial.
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
