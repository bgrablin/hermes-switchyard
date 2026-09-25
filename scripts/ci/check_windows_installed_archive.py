#!/usr/bin/env python3
"""Fail-closed Windows check of a downloaded, installed release ZIP."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_release import (  # noqa: E402
    CHECKSUMS_NAME, RELEASE_FILES, SOURCE_MANIFEST_NAME, ReleaseError, verify_archive,
)
from scripts.ci.check_native_tool_invocation import (  # noqa: E402
    _CASES, _SyntheticConnection, _validate_success,
)

PLUGIN = "hermes-switchyard"
VERSION = "0.5.4"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MEMBERS = set(RELEASE_FILES) | {SOURCE_MANIFEST_NAME, CHECKSUMS_NAME}


class WindowsGateError(RuntimeError):
    """Closed-set failure code, never raw environment or provider content."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _one_file(root: Path, name: str) -> Path:
    matches = [p for p in root.rglob(name) if p.is_file() and not p.is_symlink()]
    if len(matches) != 1:
        raise WindowsGateError("download_member_count")
    return matches[0]


def verify_downloaded_artifact(
    download_dir: Path, source_root: Path, source_sha: str,
) -> tuple[Path, bytes, str]:
    """Bind the downloaded ZIP to the Ubuntu receipt and external Git blobs."""
    if not HEX40.fullmatch(source_sha):
        raise WindowsGateError("source_sha_invalid")
    download_dir = Path(download_dir)
    if not download_dir.is_dir():
        raise WindowsGateError("download_missing")
    files = [p for p in download_dir.rglob("*") if p.is_file()]
    if any(p.is_symlink() for p in download_dir.rglob("*")) or any(
        p.name not in {f"{PLUGIN}-{VERSION}.zip", "archive-sha256.json", "release-verification.json"}
        for p in files
    ):
        raise WindowsGateError("download_extra_member")
    archive = _one_file(download_dir, f"{PLUGIN}-{VERSION}.zip")
    receipt_path = _one_file(download_dir, "archive-sha256.json")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise WindowsGateError("digest_receipt_invalid") from exc
    if not isinstance(receipt, dict) or set(receipt) != {"source_sha", "archive_sha256"}:
        raise WindowsGateError("digest_receipt_invalid")
    expected_digest = receipt["archive_sha256"]
    if receipt["source_sha"] != source_sha:
        raise WindowsGateError("source_mismatch")
    if not isinstance(expected_digest, str) or not HEX64.fullmatch(expected_digest):
        raise WindowsGateError("digest_receipt_invalid")
    data = archive.read_bytes()
    digest = _sha256(data)
    if digest != expected_digest:
        raise WindowsGateError("outer_digest_mismatch")
    try:
        result = verify_archive(
            archive, source_root=source_root, expected_source_sha=source_sha,
            expected_version=VERSION,
        )
    except ReleaseError as exc:
        raise WindowsGateError("external_source_verification_failed") from exc
    if result["verification"] != "source-verified" or _sha256(archive.read_bytes()) != digest:
        raise WindowsGateError("external_source_verification_failed")
    return archive, data, digest


def _archive_bytes(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as opened:
            infos = opened.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(MEMBERS) or set(names) != MEMBERS:
                raise WindowsGateError("archive_member_mismatch")
            if any(info.is_dir() for info in infos):
                raise WindowsGateError("archive_member_mismatch")
            return {name: opened.read(name) for name in names}
    except (OSError, zipfile.BadZipFile) as exc:
        raise WindowsGateError("archive_unreadable") from exc


def verify_installed_files(installed_root: Path, archive_data: bytes) -> dict[str, str]:
    """Require byte equality for all payload and metadata, with no extras."""
    expected = _archive_bytes(archive_data)
    root = Path(installed_root)
    if not root.is_dir() or root.is_symlink():
        raise WindowsGateError("installed_missing")
    actual: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise WindowsGateError("installed_extra_or_link")
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = path
    if set(actual) != set(expected):
        raise WindowsGateError("installed_member_mismatch")
    for name, path in actual.items():
        if path.read_bytes() != expected[name]:
            raise WindowsGateError("installed_byte_mismatch")
    return {name: _sha256(expected[name]) for name in sorted(expected)}


def install_archive(data: bytes, installed_root: Path) -> dict[str, str]:
    """Install only previously source-verified bytes in a fresh sandbox."""
    root = Path(installed_root)
    if root.exists() or root.is_symlink():
        raise WindowsGateError("install_target_not_fresh")
    members = _archive_bytes(data)
    root.mkdir(parents=True)
    for name, payload in sorted(members.items()):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return verify_installed_files(root, data)


def validate_loaded_assess(manager: Any, installed_root: Path, registry: Any) -> tuple[Any, Any]:
    """Prove normal user-plugin discovery, scoped module origin, and registry callability."""
    root = Path(installed_root).resolve()
    loaded = manager._plugins.get(PLUGIN)
    if loaded is None or not loaded.enabled or loaded.error:
        raise WindowsGateError("plugin_not_enabled")
    manifest = loaded.manifest
    if (manifest.source != "user" or manifest.name != PLUGIN or manifest.version != VERSION
            or Path(manifest.path).resolve() != root):
        raise WindowsGateError("installed_manifest_mismatch")
    module = loaded.module
    if module is None or Path(module.__file__).resolve() != root / "__init__.py":
        raise WindowsGateError("module_outside_install")
    client_name = module.__name__ + ".hermes_switchyard.client"
    client = sys.modules.get(client_name)
    if client is None or Path(client.__file__).resolve() != root / "hermes_switchyard" / "client.py":
        raise WindowsGateError("client_outside_install")
    if "jev_assess" not in manifest.provides_tools or "jev_assess" not in loaded.tools_registered:
        raise WindowsGateError("tool_not_registered")
    entry = registry.get_entry("jev_assess", scope=manager.scope_key)
    if entry is None or not callable(entry.handler):
        raise WindowsGateError("tool_not_registered")
    return entry, client


def invoke_synthetic_assess(entry: Any, client_module: Any) -> dict[str, Any]:
    """Deny sockets, then call one real registered handler with synthetic HTTPS."""
    connection = _SyntheticConnection()
    blocked: list[bool] = []

    def deny_socket(*_args: Any, **_kwargs: Any) -> None:
        blocked.append(True)
        raise OSError("network disabled during archive gate")

    case = next(case for case in _CASES if case["tool"] == "jev_assess")
    try:
        with ExitStack() as patches:
            for method in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
                if not hasattr(socket.socket, method):
                    continue
                patches.enter_context(mock.patch.object(socket.socket, method, deny_socket))
            for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
                patches.enter_context(mock.patch.object(socket, method, deny_socket))
            patches.enter_context(mock.patch.object(socket, "create_connection", deny_socket))
            patches.enter_context(mock.patch.object(
                client_module.http.client, "HTTPSConnection", return_value=connection,
            ))
            patches.enter_context(mock.patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "offline-only-synthetic-placeholder", "TYPESAFE_API_KEY": "",
            }))
            raw = entry.handler(dict(case["arguments"]))
        if blocked:
            raise WindowsGateError("network_attempt")
        if not isinstance(raw, str) or _validate_success("jev_assess", json.loads(raw)) != "assessed":
            raise WindowsGateError("assess_not_terminal")
    except WindowsGateError:
        raise
    except Exception as exc:
        raise WindowsGateError("network_attempt" if blocked else "assess_not_terminal") from exc
    if len(connection.requests) != 1:
        raise WindowsGateError("synthetic_request_count")
    return {"name": "jev_assess", "status": "assessed", "synthetic_request_count": 1,
            "real_network_connects": 0}


def _check_pinned_runtime(upstream_root: Path, upstream_sha: str) -> str:
    if not HEX40.fullmatch(upstream_sha):
        raise WindowsGateError("hermes_sha_invalid")
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if actual != upstream_sha:
            raise WindowsGateError("hermes_source_mismatch")
        import hermes_cli
        module_file = hermes_cli.__file__
        if module_file is None or not Path(module_file).resolve().is_relative_to(upstream_root.resolve()):
            raise WindowsGateError("hermes_runtime_not_pinned")
        with (upstream_root / "pyproject.toml").open("rb") as stream:
            expected_version = tomllib.load(stream)["project"]["version"]
        installed_version = importlib.metadata.version("hermes-agent")
        if installed_version != expected_version:
            raise WindowsGateError("hermes_runtime_not_pinned")
        return installed_version
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        raise WindowsGateError("hermes_runtime_not_pinned") from exc


def native_probe(installed_root: Path, upstream_root: Path, upstream_sha: str) -> dict[str, Any]:
    """Executed only from a fresh isolated Hermes interpreter by the CI job."""
    installed_version = _check_pinned_runtime(upstream_root, upstream_sha)
    blocked: list[bool] = []

    def deny_socket(*_args: Any, **_kwargs: Any) -> None:
        blocked.append(True)
        raise OSError("network disabled during plugin discovery")

    with ExitStack() as patches:
        for method in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
            if not hasattr(socket.socket, method):
                continue
            patches.enter_context(mock.patch.object(socket.socket, method, deny_socket))
        for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            patches.enter_context(mock.patch.object(socket, method, deny_socket))
        patches.enter_context(mock.patch.object(socket, "create_connection", deny_socket))
        from hermes_cli.plugins import get_plugin_manager
        from tools.registry import registry
        manager = get_plugin_manager()
        manager.discover_and_load()
        if blocked:
            raise WindowsGateError("network_attempt")
        entry, client = validate_loaded_assess(manager, installed_root, registry)
        tool = invoke_synthetic_assess(entry, client)
        if blocked:
            raise WindowsGateError("network_attempt")
    return {"hermes_version": installed_version, "tool": tool,
            "module_root": "installed:__init__.py",
            "client_root": "installed:hermes_switchyard/client.py"}


def _child_environment(home: Path) -> dict[str, str]:
    """Pass OS launch essentials, never inherited provider or GitHub credentials."""
    essentials = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC"}
    env = {key: value for key, value in os.environ.items() if key.upper() in essentials}
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_SHARED_AUTH_DIR": str(home / "shared-auth"),
        "HERMES_BUNDLED_PLUGINS": str(home / "empty-bundled"),
        "HERMES_ENABLE_PROJECT_PLUGINS": "0",
        "HOME": str(home / "user"),
        "USERPROFILE": str(home / "user"),
        "APPDATA": str(home / "user" / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "user" / "AppData" / "Local"),
        "TEMP": str(home / "temp"), "TMP": str(home / "temp"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
    })
    return env


def _windows_identity() -> dict[str, Any] | None:
    if sys.platform != "win32":
        return None
    version = sys.getwindowsversion()
    return {"major": version.major, "minor": version.minor, "build": version.build}


def run_gate(args: argparse.Namespace, report: dict[str, Any]) -> None:
    """Verify, install, spawn native process, and recheck installed bytes."""
    if sys.platform != "win32":
        raise WindowsGateError("not_windows")
    if not HEX40.fullmatch(args.source_sha) or not HEX40.fullmatch(args.upstream_sha):
        raise WindowsGateError("source_sha_invalid")
    home = args.hermes_home.resolve()
    if home.exists() or home.is_symlink() or home == Path.home().resolve():
        raise WindowsGateError("sandbox_not_fresh")
    runner_temp = os.environ.get("RUNNER_TEMP")
    runner_root = Path(runner_temp).resolve() if runner_temp else None
    if runner_root is None or home == runner_root or not home.is_relative_to(runner_root):
        raise WindowsGateError("sandbox_outside_runner_temp")
    archive, data, digest = verify_downloaded_artifact(
        args.artifact_dir, args.source_root, args.source_sha,
    )
    report["archive_sha256"] = digest
    report["archive"] = archive.name
    for part in ("empty-bundled", "shared-auth", "user", "temp"):
        (home / part).mkdir(parents=True, exist_ok=False)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-switchyard\n  disabled: []\n"
        "  entries:\n    hermes-switchyard:\n      enabled: true\n      settings:\n"
        "        automatic_skill_routing_mode: \"off\"\n"
        "        adaptive_reasoning_effort: false\n", encoding="utf-8",
    )
    installed = home / "plugins" / PLUGIN
    report["installed_sha256"] = install_archive(data, installed)
    env = _child_environment(home)
    try:
        child = subprocess.run([
            sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child-probe",
            "--installed-root", str(installed),
            "--upstream-root", str(args.upstream_root), "--upstream-sha", args.upstream_sha,
        ], cwd=home, env=env, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WindowsGateError("native_process_unavailable") from exc
    try:
        result = json.loads(child.stdout)
    except ValueError as exc:
        raise WindowsGateError("native_receipt_missing") from exc
    if not isinstance(result, dict) or child.returncode != 0 or result.get("ok") is not True:
        code = result.get("error_code") if isinstance(result, dict) else None
        allowed = {
            "native_probe_invalid", "native_probe_unexpected", "hermes_sha_invalid",
            "hermes_source_mismatch", "hermes_runtime_not_pinned", "plugin_not_enabled",
            "installed_manifest_mismatch", "module_outside_install", "client_outside_install",
            "tool_not_registered", "network_attempt", "assess_not_terminal",
            "synthetic_request_count",
        }
        raise WindowsGateError(code if code in allowed else "native_probe_failed")
    if (result.get("module_root") != "installed:__init__.py"
            or result.get("client_root") != "installed:hermes_switchyard/client.py"
            or result.get("tool") != {"name": "jev_assess", "status": "assessed",
                                      "synthetic_request_count": 1, "real_network_connects": 0}
            or not isinstance(result.get("hermes_version"), str)):
        raise WindowsGateError("native_receipt_invalid")
    report["hermes_version"] = result["hermes_version"]
    report["module_root"] = result["module_root"]
    report["client_root"] = result["client_root"]
    report["tool"] = result["tool"]
    if verify_installed_files(installed, data) != report["installed_sha256"]:
        raise WindowsGateError("installed_byte_mismatch")
    report["ok"] = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--upstream-sha")
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--child-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--installed-root", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report: dict[str, Any] = {
        "ok": False, "source_sha": args.source_sha, "archive_sha256": None,
        "installed_sha256": None, "plugin": PLUGIN, "version": VERSION,
        "hermes_source_sha": args.upstream_sha, "hermes_version": None,
        "windows_version": _windows_identity(), "python_version": platform.python_version(),
        "tool": {"name": "jev_assess", "status": "not_run", "synthetic_request_count": 0,
                 "real_network_connects": 0},
    }
    if args.child_probe:
        try:
            if sys.platform != "win32" or args.installed_root is None or args.upstream_root is None:
                raise WindowsGateError("native_probe_invalid")
            report = {"ok": True, **native_probe(args.installed_root, args.upstream_root, args.upstream_sha)}
        except WindowsGateError as exc:
            report = {"ok": False, "error_code": str(exc)}
        except Exception:
            report = {"ok": False, "error_code": "native_probe_unexpected"}
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ok"] else 1
    try:
        if any(getattr(args, key) is None for key in (
            "artifact_dir", "source_root", "source_sha", "upstream_root", "upstream_sha",
            "hermes_home", "report",
        )):
            raise WindowsGateError("arguments_missing")
        run_gate(args, report)
    except WindowsGateError as exc:
        report["error_code"] = str(exc)
    except Exception:
        report["error_code"] = "gate_unexpected"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=args.report.parent,
            prefix=".windows-archive-receipt-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, args.report)
    print(json.dumps({"ok": report["ok"], "error_code": report.get("error_code")}, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
