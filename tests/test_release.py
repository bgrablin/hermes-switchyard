"""Behavior checks for the deterministic release archive boundary."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_release import (
    RELEASE_FILES,
    ReleaseError,
    ReleaseVerificationError,
    build_release,
    verify_archive,
)


ROOT = Path(__file__).resolve().parent.parent
SOURCE_MANIFEST_NAME = "SOURCE-MANIFEST.json"
CHECKSUMS_NAME = "SHA256SUMS"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )


def _commit_fixture(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()


def _fixture_repo(base: Path) -> tuple[Path, str]:
    repo = base / "repo"
    for relative in RELEASE_FILES:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Release Fixture")
    _git(repo, "config", "user.email", "release-fixture@example.invalid")
    return repo, _commit_fixture(repo, "fixture")


def _rewrite_archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])


def _recompute_checksums(members: dict[str, bytes]) -> None:
    names = [*sorted(RELEASE_FILES), SOURCE_MANIFEST_NAME]
    members[CHECKSUMS_NAME] = (
        "\n".join(
            f"{hashlib.sha256(members[name]).hexdigest()}  {name}" for name in names
        )
        + "\n"
    ).encode("utf-8")


class ReleaseArchiveTests(unittest.TestCase):
    def test_same_commit_inputs_produce_same_archive_bytes_when_worktree_is_dirty(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, source_sha = _fixture_repo(base)
            first = build_release(repo, base / "first", source_sha)
            (repo / "README.md").write_bytes(b"dirty worktree bytes\n")
            second = build_release(repo, base / "second", source_sha)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            result = verify_archive(
                second,
                source_root=repo,
                expected_source_sha=source_sha,
                expected_version="0.3.2",
            )
            self.assertTrue(result["integrity_verified"])
            self.assertTrue(result["source_verified"])
            self.assertEqual(result["verification"], "source-verified")
            self.assertEqual(result["source_sha"], source_sha)

    def test_allowlist_excludes_forbidden_tree_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            mirror, source_sha = _fixture_repo(base)
            (mirror / ".env").write_text(
                "OPENROUTER_API_KEY=not-a-release-value\n", encoding="utf-8"
            )
            result_file = mirror / "evaluation" / "results.json"
            result_file.parent.mkdir(parents=True)
            result_file.write_text("{}\n", encoding="utf-8")
            handoff = mirror / "local-evidence" / "RELEASE-HANDOFF.md"
            handoff.parent.mkdir(parents=True)
            handoff.write_text("not a release file\n", encoding="utf-8")

            archive = build_release(mirror, base / "dist", source_sha)
            with zipfile.ZipFile(archive) as opened:
                names = set(opened.namelist())
            self.assertNotIn(".env", names)
            self.assertNotIn("evaluation/results.json", names)
            self.assertNotIn("local-evidence/RELEASE-HANDOFF.md", names)
            self.assertIn("plugin.yaml", names)
            self.assertIn("__init__.py", names)
            self.assertIn("docs/SETUP.md", names)
            self.assertIn("docs/assets/hermes-switchyard-branding.png", names)

    def test_readme_references_are_present_after_archive_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, source_sha = _fixture_repo(base)
            archive = build_release(repo, base / "dist", source_sha)
            extracted = base / "extracted"
            with zipfile.ZipFile(archive) as opened:
                opened.extractall(extracted)
            self.assertTrue((extracted / "docs/SETUP.md").is_file())
            self.assertTrue((extracted / "docs/assets/hermes-switchyard-branding.png").is_file())

    def test_source_sha_must_be_exact_lowercase_existing_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, source_sha = _fixture_repo(base)
            for malformed in (f" {source_sha}", source_sha.upper(), "b" * 40):
                with self.subTest(source_sha=malformed):
                    with self.assertRaises(ReleaseError):
                        build_release(repo, base / "dist", malformed)

    def test_symlink_allowlisted_entry_is_not_an_ordinary_blob(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink support is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, _ = _fixture_repo(base)
            target = repo / "README.md"
            target.unlink()
            try:
                target.symlink_to("LICENSE")
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            source_sha = _commit_fixture(repo, "symlink")
            with self.assertRaises(ReleaseError):
                build_release(repo, base / "dist", source_sha)

    def test_external_source_reference_rejects_recomputed_archive_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, source_sha = _fixture_repo(base)
            source_archive = build_release(repo, base / "source", source_sha)
            tampered = base / "tampered.zip"
            with zipfile.ZipFile(source_archive) as source:
                members = {name: source.read(name) for name in source.namelist()}
            members["README.md"] += b"tampered\n"
            manifest = json.loads(members[SOURCE_MANIFEST_NAME].decode("utf-8"))
            entry = next(item for item in manifest["files"] if item["path"] == "README.md")
            entry["sha256"] = hashlib.sha256(members["README.md"]).hexdigest()
            entry["size"] = len(members["README.md"])
            members[SOURCE_MANIFEST_NAME] = (
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            _recompute_checksums(members)
            _rewrite_archive(tampered, members)

            result = verify_archive(tampered, expected_source_sha=source_sha)
            self.assertTrue(result["integrity_verified"])
            self.assertFalse(result["source_verified"])
            self.assertEqual(result["verification"], "integrity-only")
            with self.assertRaises(ReleaseVerificationError):
                verify_archive(
                    tampered,
                    source_root=repo,
                    expected_source_sha=source_sha,
                )

    def test_manifest_keyset_and_redundant_metadata_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, source_sha = _fixture_repo(base)
            source_archive = build_release(repo, base / "source", source_sha)
            with zipfile.ZipFile(source_archive) as source:
                original = {name: source.read(name) for name in source.namelist()}
            mutations = (
                lambda manifest: manifest.update({"unexpected": True}),
                lambda manifest: manifest.update({"manifest_version": 2}),
                lambda manifest: manifest["files"][0].update({"unexpected": True}),
            )
            for index, mutate in enumerate(mutations):
                with self.subTest(mutation=index):
                    members = dict(original)
                    manifest = json.loads(members[SOURCE_MANIFEST_NAME].decode("utf-8"))
                    mutate(manifest)
                    members[SOURCE_MANIFEST_NAME] = (
                        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n"
                    ).encode("utf-8")
                    _recompute_checksums(members)
                    tampered = base / f"manifest-{index}.zip"
                    _rewrite_archive(tampered, members)
                    with self.assertRaises(ReleaseVerificationError):
                        verify_archive(tampered)

    def test_tampered_source_member_fails_hash_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, source_sha = _fixture_repo(base)
            source_archive = build_release(repo, base / "source", source_sha)
            tampered = base / "tampered.zip"
            with zipfile.ZipFile(source_archive) as source, zipfile.ZipFile(
                tampered, "w", zipfile.ZIP_DEFLATED
            ) as target:
                for name in source.namelist():
                    data = source.read(name)
                    if name == "README.md":
                        data += b"tampered\n"
                    target.writestr(name, data)
            with self.assertRaises(ReleaseVerificationError):
                verify_archive(tampered, expected_source_sha=source_sha)


if __name__ == "__main__":
    unittest.main()
