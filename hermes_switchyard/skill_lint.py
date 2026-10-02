"""Offline advisory lint over Hermes' name/description skill registry."""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any, cast

SCHEMA = "switchyard.lint_skills.v2"
_STOPWORDS = frozenset("a an and for in of on or the to use when with".split())
# Used only for the weak-description check, not to change the frozen overlap metric.
_GENERIC = frozenset("you need help helping various general tasks task tools tool skills skill work things assist manage useful".split())
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}\Z")
_TRIGGER = re.compile(r"Use when\b", re.IGNORECASE | re.ASCII)
_SEVERITY = {"error": 0, "warning": 1, "info": 2}
_RULES = {
    "invalid_rows": ("error", "catalog", "Some registry rows could not be checked.",
                     "Repair the registry rows identified by the reason counts, then rerun."),
    "empty_description": ("error", "description", "Description is empty or whitespace only.",
                          "Describe the task and the condition that should select this skill."),
    "low_information_description": ("warning", "description", "Description has too little specific task information.",
                                    "Name the concrete task, target, and selection condition."),
    "near_duplicate": ("warning", "overlap", "Descriptions have high lexical overlap.",
                       "Compare selection conditions and scope. Do not merge skills based on overlap alone."),
    "confusable": ("info", "overlap", "Descriptions share vocabulary; their purposes may still differ.",
                   "Check that each description distinguishes its task or tool. No change is required if clear."),
    "short_description": ("info", "style", "Description is below the optional 40-character guideline.",
                          "Keep concise descriptions if their task and scope are clear."),
    "long_description": ("info", "style", "Description exceeds the optional 200-character guideline.",
                         "Put the selection condition first; move procedural detail into the skill body."),
    "missing_use_when": ("info", "style", "Description does not start with the optional 'Use when' prefix.",
                         "Use any clear trigger or task wording; this prefix is not required by Hermes."),
}
MAX_SKILLS = 1024
MAX_DESCRIPTION_CHARS = 4096
MAX_TOKENS = 128
MAX_RESPONSE_CHARS = 8 * 1024 * 1024
MAX_PAIRS = 4096


class CatalogError(ValueError):
    """Only closed-set catalog reasons may cross the CLI boundary."""


def _tokens(description: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", description.casefold())) - _STOPWORDS


def _diagnostic(code: str, names: list[str], **evidence: Any) -> dict[str, Any]:
    severity, category, message, suggestion = _RULES[code]
    return {"code": code, "severity": severity, "category": category, "names": names,
            "message": message, "suggestion": suggestion, "evidence": evidence}


def lint_catalog(rows: Any, *, include_style: bool = False) -> dict[str, Any]:
    """Produce name-only findings from the registry's name and description fields."""
    if not isinstance(rows, list):
        raise CatalogError("invalid_catalog")
    if len(rows) > MAX_SKILLS:
        raise CatalogError("catalog_too_large")
    candidates: list[tuple[str, frozenset[str]]] = []
    findings: dict[str, dict[str, Any]] = {}
    normalized: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    invalid_reasons: Counter[str] = Counter()
    comparison_omitted = 0
    name_counts: dict[str, int] = {}
    for row in rows:
        if isinstance(row, Mapping):
            name = row.get("name")
            if type(name) is str and _NAME.fullmatch(name) is not None:
                name_counts[name] = name_counts.get(name, 0) + 1
    for row in rows:
        if not isinstance(row, Mapping):
            invalid_reasons["not_mapping"] += 1
            continue
        name, description = row.get("name"), row.get("description")
        reason = None
        if type(name) is not str or _NAME.fullmatch(name) is None:
            reason = "invalid_name"
        elif name_counts[name] > 1:
            reason = "duplicate_name"
        elif type(description) is not str:
            reason = "non_string_description"
        elif len(description) > MAX_DESCRIPTION_CHARS:
            reason = "description_too_large"
        if reason:
            invalid_reasons[reason] += 1
            continue
        name, description = cast(str, name), cast(str, description)
        words = _tokens(description)
        if len(words) > MAX_TOKENS:
            invalid_reasons["too_many_tokens"] += 1
            continue
        normalized[name] = " ".join(description.casefold().split())
        # Do not imply full-text comparison for descriptions the ASCII tokenizer
        # cannot represent. Still permit opt-in length/wording inspection.
        comparable = not any(c.isalpha() and not c.isascii() for c in description)
        if not comparable:
            comparison_omitted += 1
        issues = []
        if not description.strip():
            issues.append("empty_description")
        elif comparable and len(words - _GENERIC) < 2:
            issues.append("low_information_description")
        if comparable and not issues:
            candidates.append((name, words))
        if include_style:
            if len(description) < 40:
                issues.append("short_description")
            if len(description) > 200:
                issues.append("long_description")
            if _TRIGGER.match(description) is None:
                issues.append("missing_use_when")
        diagnostics.extend(_diagnostic(code, [name], characters=len(description)) for code in issues)
        findings[name] = {"name": name, "issues": issues, "peers": []}
    candidates.sort(key=lambda item: item[0])
    pairs: list[dict[str, Any]] = []
    edges: dict[str, set[str]] = {name: set() for name, _ in candidates}
    for index, (name, words) in enumerate(candidates):
        for peer, peer_words in candidates[index + 1:]:
            union = words | peer_words
            shared = len(words & peer_words)
            if shared < 3:
                continue
            score = shared / len(union)
            kind = "near_duplicate" if score >= 0.75 else "confusable" if score >= 0.50 else None
            if kind is None:
                continue
            if len(pairs) >= MAX_PAIRS:
                raise CatalogError("catalog_too_confusable")
            evidence = {"method": "ascii_token_jaccard", "similarity": round(score, 6),
                        "shared_tokens": shared, "union_tokens": len(union),
                        "left_only_tokens": len(words - peer_words),
                        "right_only_tokens": len(peer_words - words),
                        "exact_description": normalized[name] == normalized[peer]}
            diagnostic = _diagnostic(kind, [name, peer], **evidence)
            diagnostics.append(diagnostic)
            pairs.append({"names": [name, peer], "kind": kind,
                          "severity": diagnostic["severity"], "evidence": evidence})
            findings[name]["peers"].append({"name": peer, "kind": kind})
            findings[peer]["peers"].append({"name": name, "kind": kind})
            edges[name].add(peer)
            edges[peer].add(name)
    visited: set[str] = set()
    clusters = []
    for name, _ in candidates:
        if name in visited or not edges[name]:
            continue
        pending = [name]
        members: set[str] = set()
        while pending:
            current = pending.pop()
            if current not in members:
                members.add(current)
                pending.extend(edges[current] - members)
        visited.update(members)
        clusters.append({"names": sorted(members)})
    ordered_findings = []
    for name in sorted(findings):
        finding = findings[name]
        finding["peers"].sort(key=lambda peer: peer["name"])
        if finding["issues"] or finding["peers"]:
            ordered_findings.append(finding)
    invalid = sum(invalid_reasons.values())
    if invalid:
        diagnostics.append(_diagnostic("invalid_rows", [], rows=invalid))
    # Priority first; stronger overlap first within its severity/category.
    diagnostics.sort(key=lambda d: (_SEVERITY[d["severity"]], d["category"],
                                    -d["evidence"].get("similarity", 0), d["names"], d["code"]))
    counts = {
        "input_rows": len(rows), "skills": len(findings), "invalid_rows": invalid, "pairs": len(pairs),
        "compared_skills": len(candidates), "comparison_omitted": comparison_omitted,
        "empty_description": sum("empty_description" in entry["issues"] for entry in ordered_findings),
        "low_information_description": sum("low_information_description" in entry["issues"] for entry in ordered_findings),
        "near_duplicate": sum(pair["kind"] == "near_duplicate" for pair in pairs),
        "confusable": sum(pair["kind"] == "confusable" for pair in pairs),
        "short_description": sum("short_description" in entry["issues"] for entry in ordered_findings),
        "long_description": sum("long_description" in entry["issues"] for entry in ordered_findings),
        "missing_use_when": sum("missing_use_when" in entry["issues"] for entry in ordered_findings),
        "clusters": len(clusters),
    }
    return {"schema": SCHEMA, "status": "partial" if invalid else "ok", "counts": counts,
            "coverage_complete": not invalid, "comparison_complete": not (invalid or comparison_omitted),
            "style_enabled": include_style, "invalid_reasons": dict(sorted(invalid_reasons.items())),
            "severity_counts": {severity: sum(d["severity"] == severity for d in diagnostics) for severity in _SEVERITY},
            "diagnostics": diagnostics,
            "pairs": pairs, "clusters": clusters, "findings": ordered_findings}


def discover_report(*, include_style: bool = False) -> dict[str, Any]:
    """Ask Hermes for the already-discovered catalog, without opening a skill body."""
    from tools.skills_tool import skills_list

    raw = skills_list()
    if isinstance(raw, str) and len(raw) > MAX_RESPONSE_CHARS:
        raise CatalogError("catalog_too_large")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise CatalogError("catalog_unavailable")
    return lint_catalog(payload["skills"], include_style=include_style)


def format_report(report: dict[str, Any], *, limit: int | None = 20) -> str:
    """Bound presentation, not analysis. Never export description-derived text."""
    counts, severities = report["counts"], report["severity_counts"]
    lines = ["Offline skill routability hints (not Hermes standards lint)",
             f"Checked {counts['skills']}/{counts['input_rows']} registry rows; "
             f"compared {counts['compared_skills']} descriptions; invalid_rows: {counts['invalid_rows']}.",
             f"Findings: {severities['error']} errors, {severities['warning']} warnings, "
             f"{severities['info']} suggestions. Overlap pairs: {counts['pairs']}."]
    if not counts["input_rows"]:
        lines.append("No skills returned by the active registry; check the selected profile and project.")
    elif not severities["error"] and not severities["warning"]:
        lines.append("No actionable findings from these checks; this is not a routing-quality certificate.")
    if report["invalid_reasons"]:
        lines.append("Incomplete coverage: " + ", ".join(f"{k}={v}" for k, v in report["invalid_reasons"].items()))
    if counts["comparison_omitted"]:
        lines.append(f"ASCII comparison omitted {counts['comparison_omitted']} non-ASCII descriptions; "
                     "no collision claim is made for them.")
    if not report["style_enabled"]:
        lines.append("Style checks are off. Use --style for optional length and 'Use when' hints.")
    diagnostics = report["diagnostics"]
    visible = diagnostics if limit is None else diagnostics[:limit]
    for finding in visible:
        target = ", ".join(finding["names"]) or "catalog"
        lines.append(f"[{finding['severity']}] {target}: {finding['code']}")
        lines.append(f"  {finding['message']}")
        evidence = finding["evidence"]
        if "similarity" in evidence:
            lines.append(f"  Token overlap {evidence['similarity']:.3f} "
                         f"({evidence['shared_tokens']}/{evidence['union_tokens']}); "
                         f"exact normalized description: {str(evidence['exact_description']).lower()}.")
        lines.append(f"  {finding['suggestion']}")
    remaining = len(diagnostics) - len(visible)
    if remaining:
        lines.append(f"{remaining} more findings not shown. Use --all or --json for the complete report.")
    return "\n".join(lines)


def exit_code(report: dict[str, Any], fail_on: str | None = None) -> int:
    """Findings are advisory unless an explicit CI threshold was requested."""
    if fail_on is None:
        return 0
    return 2 if any(_SEVERITY[d["severity"]] <= _SEVERITY[fail_on] for d in report["diagnostics"]) else 0


def positive_limit(value: str) -> int:
    """Argparse type shared with the native plugin CLI setup."""
    import argparse

    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("limit must be a positive integer")
    return number
