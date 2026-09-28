"""WSL driver-store narrowing without requiring a Windows host or real GPU."""

import ctypes as C
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox import linux_policy, wsl_drivers
from sandbox import wsl_gpu_probe as probe
from sandbox.linux_gpu import NativeGPU
from sandbox.linux_mounts import MountTable
from sandbox.wsl_drivers import WSLDriverStore
from scripts.benchmark_linux_policy import policy_backend


@pytest.fixture
def store(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    root = base / "usr/lib/wsl/drivers"
    root.mkdir(parents=True)
    alias = base / "lib"
    alias.symlink_to(root.parents[1], target_is_directory=True)
    workspace = base / "project"
    workspace.mkdir()
    table = MountTable(f"1 0 1:0 / / rw - ext4 root rw\n2 1 1:1 / {root} ro - 9p drivers ro\n")
    monkeypatch.setattr(MountTable, "read", classmethod(lambda cls: table))
    monkeypatch.setattr(wsl_drivers, "DRIVER_STORE", root)
    backend = policy_backend(workspace, (base / "usr", alias))
    backend.wsl_drivers = WSLDriverStore.detect()
    package = root / "nv_example.inf_amd64_123"
    package.mkdir()
    (package / "libcuda.so.1.1").touch()
    return SimpleNamespace(
        backend=backend, root=root, alias=alias / "wsl/drivers", package=package, table=table
    )


def select(store):
    store.backend.wsl_drivers.select(json.dumps({"driver_store_paths": [str(store.package)]}))


@pytest.mark.parametrize("gpu", [False, True])
def test_unused_driver_store_is_never_enumerated_and_each_view_is_hidden(store, monkeypatch, gpu):
    for number in range(100):
        other = store.root / f"unrelated-{number}"
        other.mkdir()
        (other / ".env").touch()
    if gpu:
        select(store)
        (store.package / ".env").touch()
    opened = []
    original = linux_policy.open_directory

    def checked(path):
        canonical = Path(path).resolve()
        assert canonical != store.root
        assert not canonical.name.startswith("unrelated-")
        opened.append(canonical)
        return original(path)

    monkeypatch.setattr(linux_policy, "open_directory", checked)
    backend = store.backend
    masks, _ = backend._mount_policy(backend.read_paths, git_read=False)
    assert set(masks) == (
        {store.package / ".env", store.alias / store.package.name / ".env"} if gpu else set()
    )
    assert opened.count(store.package) == int(gpu)
    args = backend.wsl_drivers.mount_args(backend.wsl_drivers.views(backend.read_paths))
    for view in (store.root, store.alias):
        index = args.index(str(view))
        assert args[index - 1] == "--tmpfs"
        assert args[index + 1 : index + (4 if gpu else 3)] == (
            ["--ro-bind", str(store.package), str(view / store.package.name)]
            if gpu
            else ["--remount-ro", str(view)]
        )
    groups = backend.last_policy_metrics["by_root_mount"]
    assert any(group["filesystem"] == "9p" for group in groups) == gpu


def test_selected_package_is_rescanned_and_masks_override_restored_mounts(store):
    select(store)
    backend = store.backend
    backend._mount_policy(backend.read_paths, git_read=False)
    (store.package / "new.key").touch()
    assert store.package / "new.key" in backend._mount_policy(backend.read_paths, git_read=False)[0]
    backend.protected_paths = (store.root,)
    backend.executable = Path("/usr/bin/bwrap")
    backend.python = Path("/usr/bin/python3")
    backend.runtime = backend.workspace
    control = backend.workspace.parent / "control"
    control.mkdir()
    scratch = control / "scratch"
    scratch.mkdir()
    try:
        args = backend._sandbox_command(["true"], control, scratch, backend.read_paths)
        restore = args.index(str(store.package))
        mask = args.index(str(control / "hidden-dir"))
        assert restore < mask
        assert args[mask + 1] == str(store.root)
    finally:
        (control / "hidden-dir").chmod(0o700)


@pytest.mark.parametrize("bad", ["outside", "nested", "link", "protected", "missing", "empty"])
def test_untrusted_or_invalid_discovery_paths_cannot_expand_mounts(store, bad):
    path = store.package
    if bad == "outside":
        path = store.root.parent
    elif bad == "nested":
        path = store.package / "nested"
        path.mkdir()
    elif bad == "link":
        path = store.root / "alias"
        path.symlink_to(store.package, target_is_directory=True)
    elif bad == "protected":
        path = store.root / ".ssh"
        path.mkdir()
    elif bad == "missing":
        path = store.root / "gone"
    with pytest.raises(ValueError, match="无法确认"):
        store.backend.wsl_drivers.select(
            json.dumps({"driver_store_paths": [] if bad == "empty" else [str(path)]})
        )
    assert store.backend.wsl_drivers.packages == ()


def test_non_cuda_adapter_is_ignored_without_recursive_search(store):
    intel = store.root / "intel.inf_abc"
    intel.mkdir()
    store.backend.wsl_drivers.select(
        json.dumps({"driver_store_paths": [str(intel), str(store.package)]})
    )
    assert store.backend.wsl_drivers.packages == (store.package,)


@pytest.mark.parametrize("change", ["package", "mount"])
def test_driver_replacement_fails_closed(store, change):
    select(store)
    if change == "package":
        store.package.rename(store.root / "old")
        store.package.mkdir()
    else:
        store.table.mounts.clear()
    with pytest.raises(ValueError, match="发生变化"):
        store.backend._mount_policy(store.backend.read_paths, git_read=False)


def test_direct_runtime_exposure_of_package_is_rejected(store):
    with pytest.raises(ValueError, match="通用运行环境"):
        store.backend.wsl_drivers.views((store.package,))


def test_ordinary_linux_has_no_wsl_override(store, monkeypatch):
    monkeypatch.setattr(MountTable, "read", classmethod(lambda cls: MountTable("")))
    assert WSLDriverStore.detect() is None


def test_mount_table_boundary_escape_and_layout_change(monkeypatch):
    table = MountTable(
        "1 0 1:0 / / rw - ext4 root rw\n2 1 1:1 / /some\\040dir ro - 9p drivers ro\n"
    )
    assert table.containing("/some dir/child").filesystem == "9p"
    assert table.containing("/some directory").filesystem == "ext4"
    monkeypatch.setattr(MountTable, "read", classmethod(lambda cls: MountTable("")))
    with pytest.raises(ValueError, match="挂载布局"):
        table.verify()


def test_wsl_preflight_queries_inside_hidden_store_before_cuda(store, monkeypatch):
    from test_linux_native_gpu import startup_report
    from test_native_performance_metrics import outcome

    backend = store.backend
    backend.gpu = NativeGPU("wsl2", "all", ())
    backend.python = Path("/usr/bin/python3")
    backend.directory = backend.workspace
    backend.runtime = backend.workspace
    backend.healthy = True
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            assert command[-1].endswith("/sandbox/wsl_gpu_probe.py")
            assert backend.wsl_drivers.packages == ()
            return outcome(json.dumps({"driver_store_paths": [str(store.package)]}))
        assert backend.wsl_drivers.packages == (store.package,)
        return outcome(
            json.dumps(startup_report(gpu={"cuda_kernel_verified": True, "devices": ["GPU-test"]}))
        )

    monkeypatch.setattr(backend, "_run", run)
    backend._preflight()
    assert len(calls) == 2
    assert backend.preflight_metrics["driver_discovery_ms"] >= 0


@pytest.mark.parametrize("use_old", [False, True])
def test_dxcore_abi_adapter_enumeration_and_bounded_registry_query(use_old):
    def enumerate_adapters(pointer):
        request = pointer._obj
        if not request.adapters:
            request.count = 1
        else:
            request.adapters[0].handle = 42
        return 0

    text = "/usr/lib/wsl/drivers/nv_example.inf_amd64_123"
    raw = C.create_unicode_buffer(text)

    def query_adapter(pointer):
        query = pointer._obj
        assert query.handle == 42 and query.kind == 48
        registry = probe.Registry.from_address(query.data)
        assert registry.query_type == 2
        if not registry.output_size:
            registry.output_size = C.sizeof(raw)
            registry.status = 1  # Initial size-only query can report overflow.
        else:
            assert query.size >= C.sizeof(probe.Registry) + C.sizeof(raw)
            C.memmove(query.data + probe.Registry.output.offset, raw, C.sizeof(raw))
            registry.status = 0
        return 0

    lib = SimpleNamespace(D3DKMTQueryAdapterInfo=query_adapter)
    setattr(lib, "D3DKMTEnumAdapters2" if use_old else "D3DKMTEnumAdapters3", enumerate_adapters)
    assert probe.adapters(lib) == [42]
    assert probe.driver_store(lib, 42) == text


@pytest.mark.parametrize("size", [0, 1000000])
def test_dxcore_rejects_invalid_registry_allocation(size):
    def query(pointer):
        probe.Registry.from_address(pointer._obj.data).output_size = size
        return 0

    with pytest.raises(RuntimeError, match="path size"):
        probe.driver_store(SimpleNamespace(D3DKMTQueryAdapterInfo=query), 42)


def test_failed_wsl_discovery_never_starts_cuda_or_expands_mounts(store, monkeypatch):
    from test_native_performance_metrics import outcome

    backend = store.backend
    backend.gpu = NativeGPU("wsl2", "all", ())
    backend.python = Path("/usr/bin/python3")
    backend.runtime = backend.workspace
    backend.healthy = True
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return outcome("invalid discovery output")

    monkeypatch.setattr(backend, "_run", run)
    with pytest.raises(ValueError, match="无法确认"):
        backend._preflight()
    assert len(calls) == 1 and not backend.wsl_drivers.packages


def test_unsupported_display_adapter_does_not_hide_valid_cuda_package(monkeypatch):
    monkeypatch.setattr(probe.C, "CDLL", lambda path: object())
    monkeypatch.setattr(probe, "adapters", lambda lib: [1, 2])

    def query(lib, handle):
        if handle == 1:
            raise RuntimeError("Old display adapter does not support query")
        return "/usr/lib/wsl/drivers/nv_example.inf_amd64_123"

    monkeypatch.setattr(probe, "driver_store", query)
    assert probe.probe() == {"driver_store_paths": [query(None, 2)]}
    monkeypatch.setattr(probe, "adapters", lambda lib: [1])
    with pytest.raises(RuntimeError, match="No WSL driver-store paths"):
        probe.probe()
