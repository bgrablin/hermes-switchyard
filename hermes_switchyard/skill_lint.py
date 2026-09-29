"""Offline advisory lint over Hermes' name/description skill registry."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA = "switchyard.lint_skills.v1"
_STOPWORDS = frozenset("a an and for in of on or the to use when with".split())
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}\Z")
_TRIGGER = re.compile(r"Use when\b", re.IGNORECASE | re.ASCII)
MAX_SKILLS = 1024
MAX_DESCRIPTION_CHARS = 4096
MAX_TOKENS = 128
MAX_RESPONSE_CHARS = 8 * 1024 * 1024
MAX_PAIRS = 4096


class CatalogError(ValueError):
    """Only closed-set catalog reasons may cross the CLI boundary."""


def _tokens(description: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", description.casefold())) - _STOPWORDS


def lint_catalog(rows: Any) -> dict[str, Any]:
    """Produce name-only findings from the registry's name and description fields."""
    if not isinstance(rows, list):
        raise CatalogError("invalid_catalog")
    if len(rows) > MAX_SKILLS:
        raise CatalogError("catalog_too_large")
    candidates: list[tuple[str, frozenset[str]]] = []
    findings: dict[str, dict[str, Any]] = {}
    name_counts: dict[str, int] = {}
    for row in rows:
        if isinstance(row, Mapping):
            name = row.get("name")
            if type(name) is str and _NAME.fullmatch(name) is not None:
                name_counts[name] = name_counts.get(name, 0) + 1
    invalid = 0
    for row in rows:
        if not isinstance(row, Mapping):
            invalid += 1
            continue
        name, description = row.get("name"), row.get("description")
        if (
            type(name) is not str or _NAME.fullmatch(name) is None or name_counts[name] > 1
            or type(description) is not str or len(description) > MAX_DESCRIPTION_CHARS
        ):
            invalid += 1
            continue
        words = _tokens(description)
        if len(words) > MAX_TOKENS:
            invalid += 1
            continue
        candidates.append((name, words))
        issues = []
        if len(description) < 40:
            issues.append("short_description")
        if len(description) > 200:
            issues.append("long_description")
        if _TRIGGER.match(description) is None:
            issues.append("missing_use_when")
        findings[name] = {"name": name, "issues": issues, "peers": []}
    candidates.sort(key=lambda item: item[0])
    pairs: list[dict[str, Any]] = []
    edges: dict[str, set[str]] = {name: set() for name, _ in candidates}
    for index, (name, words) in enumerate(candidates):
        for peer, peer_words in candidates[index + 1:]:
            union = words | peer_words
            if not union:
                continue
            score = len(words & peer_words) / len(union)
            kind = "near_duplicate" if score >= 0.75 else "confusable" if score >= 0.50 else None
            if kind is None:
                continue
            if len(pairs) >= MAX_PAIRS:
                raise CatalogError("catalog_too_confusable")
            pairs.append({"names": [name, peer], "kind": kind})
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
    counts = {
        "skills": len(candidates), "invalid_rows": invalid, "pairs": len(pairs),
        "near_duplicate": sum(pair["kind"] == "near_duplicate" for pair in pairs),
        "confusable": sum(pair["kind"] == "confusable" for pair in pairs),
        "short_description": sum("short_description" in entry["issues"] for entry in ordered_findings),
        "long_description": sum("long_description" in entry["issues"] for entry in ordered_findings),
        "missing_use_when": sum("missing_use_when" in entry["issues"] for entry in ordered_findings),
        "clusters": len(clusters),
    }
    return {"schema": SCHEMA, "status": "ok", "counts": counts,
            "pairs": pairs, "clusters": clusters, "findings": ordered_findings}


def discover_report() -> dict[str, Any]:
    """Ask Hermes for the already-discovered catalog, without opening a skill body."""
    from tools.skills_tool import skills_list

    raw = skills_list()
    if isinstance(raw, str) and len(raw) > MAX_RESPONSE_CHARS:
        raise CatalogError("catalog_too_large")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise CatalogError("catalog_unavailable")
    return lint_catalog(payload["skills"])


def format_report(report: dict[str, Any]) -> str:
    """Keep human output bounded to names, issue types, and aggregate counts."""
    lines = ["Offline skill routability hints (not Hermes standards lint)"]
    lines.extend(f"{key}: {value}" for key, value in sorted(report["counts"].items()))
    for finding in report["findings"]:
        hints = list(finding["issues"])
        hints.extend(f"{peer['kind']} with {peer['name']}" for peer in finding["peers"])
        lines.append(f"{finding['name']}: {', '.join(hints)}")
    for group in report["clusters"]:
        lines.append("cluster: " + ", ".join(group["names"]))
    return "\n".join(lines)
