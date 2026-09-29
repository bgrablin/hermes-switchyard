"""Closed-list trivial-turn detector shared by skill routing and adaptive effort.

A greeting, thanks, or acknowledgement never names a specialist skill and needs no deep
reasoning, so a hosted call only adds latency. The rule is a closed word list, not a length
rule, so short task requests (``fix ci``) are not trivial.

Light-turn predicates (greeting-class instructions and pure read-only cwd listings)
skip hosted skill routing when a specialist skill cannot help. Open-ended explanations
stay hosted — a fixed denylist cannot prove specialist irrelevance against a dynamic
catalog. Adaptive effort still uses only ``is_trivial_turn`` / greeting-class for local
bypass.
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
# Greeting-only grammar: the ask must name a greeting/hello/hi *as the reply*,
# not merely contain those words (``Write a hello world program…`` must not match).
_GREETING_ASK_RE = re.compile(
    r"\b(?:reply|respond|say|write|give|send)\b"
    r".{0,48}"
    r"\b(?:greeting(?:\s+sentence)?|hello(?!\s+world\b)|(?<![\w-])hi)\b"
    r"|\b(?:one|a|short)\s+(?:\w+\s+){0,3}greeting(?:\s+sentence)?\b"
    r"|\bgreeting\s+sentence\b",
    re.IGNORECASE | re.DOTALL,
)
# Task / second-deliverable cues that make a greeting-shaped ask non-greeting-only.
_GREETING_CLASS_NEGATIVE_RE = re.compile(
    r"\b(?:"
    r"skill|debug|deploy|fix|patch|install|delete|browse|docker|printer|"
    r"error|plan|rollback|compose|maintenance|unreachable|logs?|"
    r"world|program|script|code|python|rust|implement|function|email|onboard|"
    r"summarize|prs?\b|pull\s+requests?|rest\s+api|api\b|issue\b|review\b"
    r")\b",
    re.IGNORECASE,
)
# Structural second deliverable: a later clause that is not a "do not …" constraint.
_SECOND_DELIVERABLE_RE = re.compile(
    r"(?:[.!?]\s+|;\s+|\n\s*|,?\s*\bthen\b\s+|,\s*\bafter\s+that\b\s+|,\s*\balso\b\s+|,\s*\bnext\b\s+)"
    r"(?!do\s+not\b|don't\b|keep\b|prefer\b|using\s+only\b|after\s+listing\b|"
    r"reply\s+with\b|finish\s+with\b)"
    r"(?:\w+\s+){0,3}"
    r"(?:"
    r"create|open|run|build|write|implement|review|list|fix|deploy|debug|"
    r"summarize|browse|patch|install|delete|edit|refactor|migrate|configure|"
    r"set\s+up|add|remove|update|fix|check|inspect|analyze|investigate"
    r")\b",
    re.IGNORECASE,
)

# Pure cwd / directory listing with an explicit no-mutation constraint.
# ``ls -la`` accepts only a missing operand or literal ``.`` / ``./`` — never a path.
_LS_LA_RE = re.compile(r"\bls\s+-la(?P<operand>\s+\S+)?", re.IGNORECASE)
_LIST_CWD_RE = re.compile(
    r"\b(?:"
    r"list\s+(?:the\s+)?(?:names?\s+of\s+)?(?:entries|files|directories|contents)"
    r"\s+(?:in\s+)?(?:the\s+)?(?:cwd|current\s+(?:working\s+)?directory)\b"
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
    r"skill|debug|deploy|printer|error\s+logs?|compose(?:\s+health)?|"
    r"docker(?:\s+update)?|rollback|unreachable|"
    r"nginx|spool|hermes|catalog|skills?|"
    # Follow-on task nouns (not the "do not patch/delete" constraint clause).
    r"analy[sz]e|inspect|investigate|vulnerabilit(?:y|ies)|security|python"
    r")\b",
    re.IGNORECASE,
)
# Any path-like token outside cwd markers rejects listing bypass.
_LIST_PATH_OPERAND_RE = re.compile(
    r"(?:^|[\s\"'`])(?:\.\./|\./[^\s\"'`]+|/[^\s\"'`]+|~/[^\s\"'`]+|[A-Za-z]:\\)",
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
# Domain / consequential cues: if a specialist skill might improve the answer, host.
# Capability-first: wall-time savings must not skip printer/docker/debug/logs/k8s routing.
# A fixed list cannot prove "no skill helps"; unexplained infra/product nouns fail closed.
_EXPLAIN_CONSEQUENTIAL_RE = re.compile(
    r"\b(?:"
    r"plan\s+a|prioritize\s+risk|rollback|production|maintenance\s+window|"
    r"home-lab|unreachable|ERROR\s+lines|"
    r"debug\w*|diagnos\w*|printer|docker|compose|deploy|ci\b|logs?|network|"
    r"fix|patch|install|error|healthcheck|pytest|flaky|nginx|spool|"
    r"systemd|redeploy|failure\s+mode|"
    # Platforms / infra nouns Copilot called out (and close neighbors).
    r"k8s|kubernetes|ingress|egress|helm|istio|terraform|ansible|puppet|"
    r"aws|gcp|azure|lambda|kafka|redis|postgres(?:ql)?|mysql|mongodb|mongo|"
    r"prometheus|grafana|vault|okta|oauth|sso|cidr|vpc|subnet|firewall|dns|"
    r"tls|ssl|certbot|letsencrypt|cloudflare|traefik|haproxy|envoy|"
    r"pod\b|nodes?|cluster|namespace|sidecar|mesh\b|ci/?cd|devops|"
    r"jenkins|github\s+actions|gitlab|circleci|argocd|flux\b"
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
    A second deliverable (``Then create a REST API``) fails closed structurally.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _LIGHT_TURN_MAX_CHARS:
        return False
    if is_trivial_turn(stripped):
        return True
    if not _GREETING_ASK_RE.search(stripped):
        return False
    if _GREETING_CLASS_NEGATIVE_RE.search(stripped):
        return False
    if _SECOND_DELIVERABLE_RE.search(stripped):
        return False
    return True


def _ls_la_is_cwd_only(text: str) -> bool:
    """True when every ``ls -la`` occurrence has no operand or only ``.`` / ``./``."""
    found = False
    for match in _LS_LA_RE.finditer(text):
        found = True
        operand = (match.group("operand") or "").strip()
        if operand and operand not in {".", "./"}:
            return False
    return found


def is_readonly_listing_prompt(text: str) -> bool:
    """True for a pure cwd listing request with an explicit no-mutation rule.

    Specialist skills cannot help ``ls -la .`` / list-cwd turns; hosted skill
    selection only adds latency. Path-scoped listings (``ls -la /etc``, compose
    dirs, log trees) stay hosted so domain skills can still route.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _LIGHT_TURN_MAX_CHARS:
        return False
    has_ls = _ls_la_is_cwd_only(stripped)
    has_list = _LIST_CWD_RE.search(stripped) is not None
    if not (has_ls or has_list):
        return False
    # ``ls -la /etc`` yields has_ls False; also reject leakage of path operands
    # next to an otherwise cwd-shaped list request.
    if _LIST_PATH_OPERAND_RE.search(stripped):
        return False
    if not _NO_MUTATE_RE.search(stripped):
        return False
    # Listing must be the only deliverable — follow-on analyze/debug tasks stay hosted.
    if _SECOND_DELIVERABLE_RE.search(stripped):
        return False
    return _LIST_NEGATIVE_RE.search(stripped) is None


def is_light_explanation_prompt(text: str) -> bool:
    """True for a short no-action explanation that does not ask to load a skill.

    Covers light multi-step Q&A without domain-skill nouns. Domain explanations
    (docker/printer/debug/logs/deploy/k8s/…) stay hosted — capability over wall-time.
    A fixed denylist cannot prove no skill helps; unexplained infra nouns fail closed.
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
    greeting-class instructions and pure read-only cwd listings where a specialist
    skill cannot earn its keep. Open-ended explanations are not bypassed by default.
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
    # Open-ended explanations stay hosted: a fixed denylist cannot prove a
    # specialist skill is irrelevant against a dynamic catalog (Copilot #164).
    return None
