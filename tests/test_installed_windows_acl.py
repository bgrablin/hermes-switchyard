import builtins
import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_plugin_load_invokes_windows_acl_without_source_import(monkeypatch, tmp_path):
    package_name = f"installed_plugin_{uuid.uuid4().hex}"
    package = ROOT / "hermes_switchyard"
    package_spec = importlib.util.spec_from_file_location(
        package_name, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    loaded_package = importlib.util.module_from_spec(package_spec)
    monkeypatch.setitem(sys.modules, package_name, loaded_package)
    package_spec.loader.exec_module(loaded_package)

    acl_spec = importlib.util.spec_from_file_location(f"{package_name}._win_acl", package / "_win_acl.py")
    acl_module = importlib.util.module_from_spec(acl_spec)
    monkeypatch.setitem(sys.modules, acl_spec.name, acl_module)
    acl_spec.loader.exec_module(acl_module)
    source_spec = importlib.util.spec_from_file_location(
        f"{package_name}.receipt_state", package / "receipt_state.py"
    )
    module = importlib.util.module_from_spec(source_spec)
    monkeypatch.setitem(sys.modules, source_spec.name, module)
    source_spec.loader.exec_module(module)

    real_import = builtins.__import__

    def block_original_package(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_switchyard":
            raise ModuleNotFoundError("original package name is unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", block_original_package)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    invoked = []
    monkeypatch.setattr(acl_module, "set_private_dacl", invoked.append)

    target = tmp_path / "receipt.tmp"
    module._apply_private_permissions(target)
    assert invoked == [target]
