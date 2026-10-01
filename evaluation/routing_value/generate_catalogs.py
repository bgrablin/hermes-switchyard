"""Build deterministic, synthetic skill catalogs and paired routing-value tasks."""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

SEED = 130
SIZES = (25, 150, 600)
CLUSTER = (
    ("shade", "shaded rest stops"),
    ("parking", "visitor parking"),
    ("signage", "direction signs"),
    ("weather", "rain plan"),
    ("access", "step-free access"),
    ("volunteers", "volunteer check-in"),
    ("refreshments", "water stations"),
    ("timing", "walk start times"),
)
CORE = (
    ("gardening", "seed-swap-inventory", "seed swap inventory"),
    ("libraries", "reading-circle-seating", "reading circle seating"),
    ("cooking", "picnic-menu-cards", "picnic menu cards"),
    ("arts", "mural-workshop-layout", "mural workshop layout"),
    ("music", "choir-practice-order", "choir practice order"),
    ("education", "field-trip-stations", "field trip stations"),
    ("recreation", "board-game-pairings", "board game pairings"),
    ("writing", "story-circle-prompts", "story circle prompts"),
    ("crafts", "paper-folding-lesson", "paper folding lesson"),
    ("cycling", "family-ride-stops", "family ride stops"),
    ("walking", "nature-walk-notes", "nature walk notes"),
    ("photography", "photo-walk-themes", "photo walk themes"),
    ("household", "shared-chores-rota", "shared chores rota"),
    ("community", "block-party-seating", "block party seating"),
    ("fitness", "stretch-class-sequence", "stretch class sequence"),
    ("travel", "day-trip-packing", "day trip packing"),
    ("sewing", "fabric-scrap-sorting", "fabric scrap sorting"),
)
TOPICS = {
    "gardening": ("seedlings", "compost", "planters"),
    "libraries": ("book clubs", "story hours", "shelves"),
    "cooking": ("picnics", "bread", "soups"),
    "arts": ("murals", "sketches", "collage"),
    "music": ("choirs", "rhythm", "practice"),
    "education": ("lessons", "field trips", "study groups"),
    "recreation": ("games", "puzzles", "play days"),
    "writing": ("journals", "stories", "letters"),
    "crafts": ("paper crafts", "clay", "weaving"),
    "cycling": ("rides", "bike checks", "rest stops"),
    "walking": ("trails", "walk groups", "nature notes"),
    "photography": ("portraits", "photo walks", "framing"),
    "household": ("laundry", "chores", "storage"),
    "community": ("gatherings", "volunteers", "neighbors"),
    "fitness": ("stretches", "warmups", "cooldowns"),
    "travel": ("packing", "day trips", "itineraries"),
    "sewing": ("patterns", "repairs", "fabrics"),
    "nature": ("birdwatching", "wildflowers", "pond visits"),
    "theater": ("rehearsals", "props", "readings"),
    "history": ("exhibits", "oral stories", "walking tours"),
}
ACTIONS = ("checklist", "calendar", "guide", "worksheet", "brief", "map", "plan", "log", "schedule", "reference")


def _slug(text: str) -> str:
    return text.replace(" ", "-")


def _code(rng: random.Random) -> str:
    return f"qx-{rng.getrandbits(64):016x}"


def _definitions() -> list[dict[str, str]]:
    skills = [
        {
            "category": "community-events", "name": f"garden-walk-{focus}",
            "subject": subject, "description": f"Use for fictional Garden Walk {subject}; not other Garden Walk logistics.",
        }
        for focus, subject in CLUSTER
    ]
    skills += [
        {
            "category": category, "name": name, "subject": subject,
            "description": f"Use for the {subject} in a fictional community workshop.",
        }
        for category, name, subject in CORE
    ]
    extras = [
        {
            "category": category, "name": f"{category}-{_slug(topic)}-{action}",
            "subject": f"{topic} {action}",
            "description": f"Use for a {action} about {topic} in a fictional {category} exercise.",
        }
        for category, topics in TOPICS.items() for topic in topics for action in ACTIONS
    ]
    random.Random(SEED).shuffle(extras)
    return skills + extras[: SIZES[-1] - len(skills)]


def _task_templates() -> tuple[list[dict], dict[str, list[tuple[str, str]]]]:
    rng = random.Random(SEED)
    tasks: list[dict] = []
    facts: dict[str, list[tuple[str, str]]] = {}

    def add_fact(name: str, case: str) -> str:
        token = _code(rng)
        facts.setdefault(name, []).append((case, token))
        return token

    for index in range(20):
        skill = CORE[index % len(CORE)]
        token = add_fact(skill[1], f"scenario {index + 1}")
        tasks.append({
            "schema": "switchyard-eval-task/1", "id": f"rv-{index + 1:03}",
            "category": "hidden_fact",
            "prompt": (f"For the fictional {skill[2]} workshop, give the invented label for "
                       f"scenario {index + 1}. Reply with the label only."),
            "expected_skill": skill[1], "checker": {"kind": "all_terms", "terms": [token]},
        })

    for index in range(8):
        tasks.append({
            "schema": "switchyard-eval-task/1", "id": f"rv-{index + 21:03}",
            "category": "no_skill_needed",
            "prompt": f"Copy this complete sentence exactly, with no extra text: The paper kite number {index + 1} is blue.",
            "expected_skill": None,
            "checker": {"kind": "exact", "value": f"The paper kite number {index + 1} is blue.",
                        "forbid_terms": [tasks[index]["checker"]["terms"][0]]},
        })

    for index, (focus, subject) in enumerate(CLUSTER):
        name = f"garden-walk-{focus}"
        token = add_fact(name, subject)
        tasks.append({
            "schema": "switchyard-eval-task/1", "id": f"rv-{index + 29:03}",
            "category": "ambiguous",
            "prompt": f"For the fictional Garden Walk, what is the invented label for {subject}? Reply with the label only.",
            "expected_skill": name, "checker": {"kind": "all_terms", "terms": [token]},
        })

    for index in range(4):
        first, second = CORE[index], CORE[index + 8]
        first_token = add_fact(first[1], f"coordination case {index + 1}")
        second_token = add_fact(second[1], f"coordination case {index + 1}")
        tasks.append({
            "schema": "switchyard-eval-task/1", "id": f"rv-{index + 37:03}",
            "category": "multi_skill",
            "prompt": (f"For the fictional workshop, give the invented coordination labels for "
                       f"{first[2]} and {second[2]} for coordination case {index + 1}, "
                       "in that order. Reply with both labels only."),
            "expected_skill": [first[1], second[1]],
            "checker": {"kind": "all_terms", "terms": [first_token, second_token]},
        })
    return tasks, facts


def generate(root: Path) -> None:
    """Write the three nested catalogs and their shared task variants under root."""
    definitions = _definitions()
    tasks, facts = _task_templates()
    root.mkdir(parents=True, exist_ok=True)
    catalogs = root / "catalogs"
    if catalogs.is_symlink():
        raise ValueError("refusing to replace a symlinked catalogs directory")
    if catalogs.exists():
        shutil.rmtree(catalogs)
    for size in SIZES:
        for definition in definitions[:size]:
            name = definition["name"]
            skill = catalogs / f"c{size}" / "skills" / definition["category"] / name / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            frontmatter = ("---\n" + f"name: {name}\n"
                           + "description: " + json.dumps(definition["description"]) + "\n---\n\n")
            body = f"# {name.replace('-', ' ').title()}\n\nUse for {definition['subject']} in a fictional civilian exercise.\n"
            for case, token in facts.get(name, ()):
                body += f"The invented label for {case} is {token}. Quote it only for this case.\n"
            skill.write_text(frontmatter + body, encoding="utf-8", newline="\n")
    variants = [{**task, "skills_dir": f"catalogs/c{size}", "toolsets": ["skills"]}
                for size in SIZES for task in tasks]
    (root / "tasks.json").write_text(json.dumps(variants, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    generate(args.root)


if __name__ == "__main__":
    main()
