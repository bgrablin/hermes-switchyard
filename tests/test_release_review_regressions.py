"""Regression coverage for exact source identity and release boundaries."""
import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.build_release import ReleaseError, _has_register_binding, _source_files_from_git
from scripts.check_portability import _history_failures


class ReviewRegressions(unittest.TestCase):
    def test_register_requires_sync_definition_or_reexport(self):
        for source in ('async def register(ctx): pass', 'import register', 'import os as register'):
            self.assertFalse(_has_register_binding(ast.parse(source)))
        for source in ('def register(ctx): pass', 'from package import register', 'from package import setup as register'):
            self.assertTrue(_has_register_binding(ast.parse(source)))

    def test_tag_object_is_not_a_source_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-q')
            git('config', 'user.name', 'bgrablin')
            git('config', 'user.email', '5216789+bgrablin@users.noreply.github.com')
            git('-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'Fixture')
            git('-c', 'tag.gpgsign=false', 'tag', '-a', 'fixture', '-m', 'Fixture tag')
            tag = git('rev-parse', 'refs/tags/fixture')
            self.assertEqual(git('cat-file', '-t', tag), 'tag')
            with self.assertRaisesRegex(ReleaseError, 'commit object directly'):
                _source_files_from_git(root, tag)

    def test_deleted_operational_files_remain_history_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(['git', '-C', directory, *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git('init', '-q')
            git('config', 'user.name', 'bgrablin')
            git('config', 'user.email', '5216789+bgrablin@users.noreply.github.com')
            path = root / 'trace.log'
            path.write_text('benign fixture text\n')
            git('add', 'trace.log')
            git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'Add fixture')
            git('rm', 'trace.log')
            git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'Remove fixture')
            self.assertTrue(any('operational log file' in item for item in _history_failures(root, set())))
