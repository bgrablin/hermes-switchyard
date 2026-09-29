"""Offline provenance, Hermes-parser, and regeneration checks for routing fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

if __package__:
    from .generate_catalogs import SIZES, generate
else:
    from generate_catalogs import SIZES, generate

DISTRIBUTION = {"hidden_fact": 20, "no_skill_needed": 8, "ambiguous": 8, "multi_skill": 4}
TOKEN = re.compile(r"qx-[0-9a-f]{16}\Z")
TOKEN_IN_TEXT = re.compile(r"qx-[0-9a-f]{16}")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _catalog_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _tree_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        digest.update(path.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()


def _skills(catalog: Path) -> dict[str, str]:
    # These are the public parser and discovery functions used by Hermes itself.
    from agent.skill_utils import iter_skill_index_files, parse_frontmatter, skill_matches_platform

    skills_root = catalog / "skills"
    disk_files = set(skills_root.rglob("SKILL.md"))
    discovered = set(iter_skill_index_files(skills_root, "SKILL.md"))
    if disk_files != discovered:
        raise ValueError(f"{catalog.name}: Hermes did not discover every skill")
    bodies: dict[str, str] = {}
    for path in sorted(discovered):
        relative = path.relative_to(skills_root)
        if len(relative.parts) != 3:
            raise ValueError(f"{catalog.name}: invalid skill tree")
        content = path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        name, description = frontmatter.get("name"), frontmatter.get("description")
        if (name != path.parent.name or not isinstance(description, str) or not description.strip()
                or not body.strip() or not skill_matches_platform(frontmatter) or name in bodies):
            raise ValueError(f"{catalog.name}: skill does not load with usable Hermes metadata: {path.parent.name}")
        if TOKEN_IN_TEXT.search(name + description):
            raise ValueError(f"{catalog.name}: hidden fact leaked into skill index metadata: {name}")
        bodies[name] = body.casefold()
    return bodies


def _terms_for(task: dict, skills: dict[str, str]) -> None:
    checker = task.get("checker")
    if not isinstance(checker, dict):
        raise ValueError(f"{task['id']}: missing checker")
    kind = checker.get("kind")
    if kind not in {"all_terms", "regex", "exact"}:
        raise ValueError(f"{task['id']}: unsupported checker")
    expected = task.get("expected_skill")
    category = task.get("category")
    if category == "no_skill_needed":
        if expected is not None or kind != "exact" or not isinstance(checker.get("value"), str):
            raise ValueError(f"{task['id']}: no-skill case must be exact with no target skill")
    elif category == "multi_skill":
        if not isinstance(expected, list) or len(expected) != 2 or len(set(expected)) != 2:
            raise ValueError(f"{task['id']}: multi-skill case requires two distinct names")
    elif not isinstance(expected, str):
        raise ValueError(f"{task['id']}: missing skill name")
    names = expected if isinstance(expected, list) else ([expected] if expected else [])
    if not set(names).issubset(skills):
        raise ValueError(f"{task['id']}: expected skill does not resolve")
    terms = checker.get("terms", [])
    if category != "no_skill_needed" and (not isinstance(terms, list) or len(terms) != len(names)):
        raise ValueError(f"{task['id']}: one hidden fact is required per expected skill")
    if kind == "regex":
        pattern = checker.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{task['id']}: missing regular expression")
        re.compile(pattern)
    if kind == "exact" and category != "no_skill_needed" and not isinstance(checker.get("value"), str):
        raise ValueError(f"{task['id']}: missing exact answer")
    prompt = task["prompt"].casefold()
    for term, name in zip(terms, names):
        if not isinstance(term, str) or not TOKEN.fullmatch(term) or term.casefold() in prompt:
            raise ValueError(f"{task['id']}: hidden fact is invalid or appears in the prompt")
        owners = [skill for skill, body in skills.items() if term.casefold() in body]
        if owners != [name]:
            raise ValueError(f"{task['id']}: hidden fact must occur only in {name}")
        if kind == "exact" and term.casefold() not in checker["value"].casefold():
            raise ValueError(f"{task['id']}: exact answer does not require its hidden fact")
    forbidden = checker.get("forbid_terms", [])
    if not isinstance(forbidden, list) or (category == "no_skill_needed" and not forbidden):
        raise ValueError(f"{task['id']}: missing forbidden facts")
    for term in forbidden:
        if not isinstance(term, str) or not TOKEN.fullmatch(term) or term.casefold() in prompt:
            raise ValueError(f"{task['id']}: invalid forbidden fact or prompt leak")
        if not any(term.casefold() in body for body in skills.values()):
            raise ValueError(f"{task['id']}: forbidden fact is absent from catalog")
        if kind == "exact" and term.casefold() in checker["value"].casefold():
            raise ValueError(f"{task['id']}: exact answer includes forbidden fact")


def validate(root: Path) -> dict:
    """Return an aggregate receipt, or raise on any fixture or byte-level mismatch."""
    tasks_path = root / "tasks.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        raise ValueError("tasks.json must be a list")
    catalogs: dict[str, dict[str, str]] = {}
    for size in SIZES:
        key = f"c{size}"
        catalogs[key] = _skills(root / "catalogs" / key)
        if len(catalogs[key]) != size:
            raise ValueError(f"{key}: incorrect skill count")
    shared: dict[str, dict] = {}
    counts = Counter()
    category_counts: dict[str, Counter] = {key: Counter() for key in catalogs}
    for task in tasks:
        if not isinstance(task, dict) or task.get("schema") != "switchyard-eval-task/1":
            raise ValueError("invalid task schema")
        task_id = task.get("id")
        skills_dir = task.get("skills_dir")
        if (not isinstance(task_id, str) or not re.fullmatch(r"rv-\d{3}", task_id)
                or skills_dir not in {f"catalogs/{key}" for key in catalogs}
                or not isinstance(task.get("prompt"), str) or not task["prompt"]
                or task.get("toolsets") != ["skills"]
                or task.get("category") not in DISTRIBUTION):
            raise ValueError(f"{task_id}: invalid fixture record")
        key = skills_dir.removeprefix("catalogs/")
        pair = (key, task_id)
        counts[pair] += 1
        if counts[pair] != 1:
            raise ValueError(f"{task_id}: duplicate task in {key}")
        _terms_for(task, catalogs[key])
        category_counts[key][task["category"]] += 1
        common = {field: value for field, value in task.items() if field != "skills_dir"}
        if task_id in shared and shared[task_id] != common:
            raise ValueError(f"{task_id}: shared prompt, fact or checker differs by catalog")
        shared[task_id] = common
    if any(dict(category_counts[key]) != DISTRIBUTION for key in catalogs) or len(shared) != sum(DISTRIBUTION.values()):
        raise ValueError("task category distribution or shared IDs differ")

    # Regeneration is compared without touching committed inputs; extra/missing files also fail.
    with tempfile.TemporaryDirectory(prefix="routing-value-") as directory:
        regenerated = Path(directory)
        generate(regenerated)
        if tasks_path.read_bytes() != (regenerated / "tasks.json").read_bytes():
            raise ValueError("tasks.json differs from deterministic generation")
        for size in SIZES:
            key = f"c{size}"
            if _catalog_bytes(root / "catalogs" / key) != _catalog_bytes(regenerated / "catalogs" / key):
                raise ValueError(f"{key}: catalog differs from deterministic regeneration")
    return {
        "schema": "switchyard-routing-value-receipt/1",
        "task_count": len(tasks), "shared_task_count": len(shared),
        "catalog_counts": {key: len(skills) for key, skills in catalogs.items()},
        "category_counts": {key: dict(counts) for key, counts in category_counts.items()},
        "catalog_sha256": {key: _tree_digest(_catalog_bytes(root / "catalogs" / key)) for key in catalogs},
        "tasks_sha256": _digest(tasks_path.read_bytes()),
        "hermes_parser": "agent.skill_utils.parse_frontmatter + iter_skill_index_files",
        "regeneration_identical": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--receipt", type=Path, help="write aggregate JSON receipt outside the fixture tree")
    args = parser.parse_args()
    try:
        receipt = validate(args.root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"fixture validation failed: {exc}")
    serialized = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    if args.receipt:
        args.receipt.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
