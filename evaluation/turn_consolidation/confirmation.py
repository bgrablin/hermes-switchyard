"""Independent confirmation using existing hidden-fact routing fixtures.
These labels require a skill because the requested fact exists only in its body.
Question wording, thresholds, repeated arms and transport stay frozen.
"""

import json
import screen

root = screen.REPO / "evaluation/routing_value"
tasks = json.loads((root / "tasks.json").read_text())
small = [t for t in tasks if t["skills_dir"] == "catalogs/c25"]
hidden = [t for t in small if t["category"] == "hidden_fact"][:12]
plain = [t for t in small if t["category"] == "no_skill_needed"][:4]
screen.CASES = [
    (t["id"], t["prompt"], t["expected_skill"], False) for t in hidden + plain
]
screen.CATALOG = [
    {"name": p.parent.name, "description": p.parent.name}
    for p in sorted((root / "catalogs/c25/skills").rglob("SKILL.md"))
]
assert len(screen.CASES) == 16 and len(screen.CATALOG) == 25
if __name__ == "__main__":
    screen.main()
