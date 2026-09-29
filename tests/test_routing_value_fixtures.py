"""Offline behavior checks for the synthetic skill-routing fixtures (#130)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class RoutingValueFixturesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_generation_is_deterministic_and_catalogs_are_nested(self) -> None:
        from evaluation.routing_value.generate_catalogs import generate

        first, second = self.tmp_path / "first", self.tmp_path / "second"
        generate(first)
        generate(second)
        self.assertEqual(_file_bytes(first), _file_bytes(second))

        tasks = json.loads((first / "tasks.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(tasks), 120)
        for size in (25, 150, 600):
            catalog = first / "catalogs" / f"c{size}"
            skills = list((catalog / "skills").glob("*/*/SKILL.md"))
            self.assertEqual(len(skills), size)
            size_tasks = [task for task in tasks if task["skills_dir"] == f"catalogs/c{size}"]
            self.assertGreaterEqual(len(size_tasks), 40)
            self.assertEqual({task["category"] for task in size_tasks},
                             {"hidden_fact", "no_skill_needed", "ambiguous", "multi_skill"})

        first_case = next(task for task in tasks if task["skills_dir"] == "catalogs/c25")
        body = next((first / "catalogs/c25/skills").glob(f"*/{first_case['expected_skill']}/SKILL.md")).read_text(encoding="utf-8")
        self.assertIn("scenario 1", body)
        self.assertIn(first_case["checker"]["terms"][0], body)

    def test_validator_rejects_fact_leaked_into_another_skill(self) -> None:
        from evaluation.routing_value.generate_catalogs import generate

        root = self.tmp_path / "fixtures"
        generate(root)
        tasks = json.loads((root / "tasks.json").read_text(encoding="utf-8"))
        target = next(task for task in tasks if task["category"] == "hidden_fact" and task["skills_dir"] == "catalogs/c25")
        fact = target["checker"]["terms"][0]
        expected = target["expected_skill"]
        other = next(path for path in (root / "catalogs/c25/skills").glob("*/*/SKILL.md") if path.parent.name != expected)
        other.write_text(other.read_text(encoding="utf-8") + f"\n{fact}\n", encoding="utf-8")

        script = Path(__file__).resolve().parents[1] / "evaluation/routing_value/validate_fixtures.py"
        result = subprocess.run([sys.executable, str(script), "--root", str(root)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(target["id"], result.stderr)

    def test_validator_rejects_fact_in_skill_index_description(self) -> None:
        from evaluation.routing_value.generate_catalogs import generate
        from evaluation.routing_value.validate_fixtures import validate

        root = self.tmp_path / "fixtures"
        generate(root)
        task = json.loads((root / "tasks.json").read_text(encoding="utf-8"))[0]
        skill = next((root / "catalogs/c25/skills").glob(f"*/{task['expected_skill']}/SKILL.md"))
        original = skill.read_text(encoding="utf-8")
        leaked = task["checker"]["terms"][0]
        skill.write_text(original.replace('description: "', f'description: "{leaked} ', 1), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "index metadata"):
            validate(root)

    def test_validator_accepts_generated_fixtures_and_emits_digest_receipt(self) -> None:
        from evaluation.routing_value.generate_catalogs import generate
        from evaluation.routing_value.validate_fixtures import validate

        root = self.tmp_path / "fixtures"
        generate(root)
        receipt = validate(root)
        self.assertGreaterEqual(receipt["task_count"], 120)
        self.assertEqual(receipt["catalog_counts"], {"c25": 25, "c150": 150, "c600": 600})
        self.assertEqual(len(receipt["catalog_sha256"]), 3)
        self.assertEqual(len(receipt["tasks_sha256"]), 64)

    def test_validator_rejects_prompt_leak_even_with_matching_catalog(self) -> None:
        from evaluation.routing_value.generate_catalogs import generate
        from evaluation.routing_value.validate_fixtures import validate

        root = self.tmp_path / "fixtures"
        generate(root)
        tasks_path = root / "tasks.json"
        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
        target = next(task for task in tasks if task["category"] == "multi_skill")
        target["prompt"] += " " + target["checker"]["terms"][1]
        tasks_path.write_text(json.dumps(tasks), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, target["id"]):
            validate(root)
