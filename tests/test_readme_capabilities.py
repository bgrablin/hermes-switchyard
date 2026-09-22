"""Contract tests for the README capability inventory.

The README once described six tools while the plugin registered seven, and it did
not present the hooks or the middleware as deployed surface. These tests read the
real registration data, so the same drift fails the suite instead of shipping.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from hermes_switchyard import TOOL_TOOLSETS

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PLUGIN_MANIFEST = ROOT / "plugin.yaml"

# Small counts only. A count outside this map fails the test instead of guessing.
_NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    return text[start : text.index(next_heading, start)]


def _features_section(text: str) -> str:
    return _section(text, "## Supported features", "## Automatic skill recommendations")


def _toolsets_section(text: str) -> str:
    return _section(text, "## Toolsets and session exposure", "## Configuration")


def _manifest_list(name: str) -> list[str]:
    """Read one block list from plugin.yaml. The offline tests use no YAML parser."""
    items: list[str] = []
    in_block = False
    for line in PLUGIN_MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}:"):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.startswith(" "):
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
        elif stripped:
            break
    return items


class ReadmeCapabilityInventoryTests(unittest.TestCase):
    def test_toolsets_table_matches_the_registered_tool_mapping(self):
        table: dict[str, str] = {}
        for line in _toolsets_section(_readme_text()).splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            toolset = re.fullmatch(r"`([a-z_]+)`", cells[0])
            if toolset is None:
                continue
            for tool in re.findall(r"`(jev_[a-z_]+)`", cells[1]):
                table[tool] = toolset.group(1)
        self.assertEqual(
            table,
            dict(TOOL_TOOLSETS),
            "the README toolsets table and hermes_switchyard.TOOL_TOOLSETS disagree",
        )

    def test_tool_and_capability_counts_match_the_registered_surface(self):
        section = _toolsets_section(_readme_text())
        features = _features_section(_readme_text())
        total = _NUMBER_WORDS[len(TOOL_TOOLSETS)]
        decision = _NUMBER_WORDS[
            sum(1 for toolset in TOOL_TOOLSETS.values() if toolset == "hermes_switchyard")
        ]
        hooks = _manifest_list("provides_hooks")
        middleware = _manifest_list("provides_middleware")
        self.assertIn(f"its {total} tools", section)
        self.assertIn(f"the {decision} decision tools", section)
        self.assertIn(f"none of the {total} tools", section)
        self.assertIn(f"To expose all {total}", section)
        self.assertIn(f"| Tools ({len(TOOL_TOOLSETS)}) |", features)
        self.assertIn(f"| Hooks ({len(hooks)}) |", features)
        self.assertIn(f"| Middleware ({len(middleware)}) |", features)
        for word in sorted(set(_NUMBER_WORDS.values()) - {total, decision}):
            self.assertIsNone(
                re.search(rf"\b{word} tools\b", section),
                f"the README toolsets section still carries the count '{word} tools'",
            )
        for word in sorted(set(_NUMBER_WORDS.values()) - {decision}):
            self.assertIsNone(
                re.search(rf"\b{word} decision tools\b", section),
                f"the README toolsets section still carries the count '{word} decision tools'",
            )

    def test_capability_table_lists_every_declared_hook_and_middleware(self):
        features = _features_section(_readme_text())
        for name in _manifest_list("provides_hooks") + _manifest_list("provides_middleware"):
            self.assertIn(
                f"`{name}`",
                features,
                f"the README capability inventory does not list {name}",
            )

    def test_declared_hooks_and_middleware_are_registered_in_the_source(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "hermes_switchyard").rglob("*.py"))
        )
        for name in _manifest_list("provides_hooks") + _manifest_list("provides_middleware"):
            self.assertIn(
                f'"{name}"',
                source,
                f"plugin.yaml declares {name} but the package source does not register it",
            )


if __name__ == "__main__":
    unittest.main()