"""Private ACL preparation reuses descriptors without relaxing link checks."""

import os
import stat
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from host_support import windows_security as security_module
from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from host_support.windows_security import PrivateWindowsSecurity


@pytest.fixture
def sealing(monkeypatch, tmp_path):
    security = object.__new__(PrivateWindowsSecurity)
    events = []
    descriptor = object()

    @contextmanager
    def dacl(sid, *, writable):
        events.append(("open", sid, writable))
        try:
            yield descriptor
        finally:
            events.append(("free",))

    def set_acl(path, dacl, **kwargs):
        assert dacl is descriptor
        events.append(("seal", path, kwargs))

    monkeypatch.setattr(security, "_dacl", dacl)
    monkeypatch.setattr(security, "_set_acl", set_acl)
    monkeypatch.setattr(
        security_module,
        "walk_descriptors",
        lambda _: iter([(tmp_path, ["sub"], ["a.py"], 101), (tmp_path / "sub", [], ["b.py"], 202)]),
    )
    monkeypatch.setattr(
        security_module.windows_files,
        "stat_at",
        lambda name, **kw: SimpleNamespace(
            st_mode=stat.S_IFDIR if name == "sub" else stat.S_IFREG, st_nlink=1
        ),
    )
    return security, events, tmp_path


def test_tree_reuses_one_descriptor_and_pinned_parent_handles(sealing):
    security, events, root = sealing
    security.seal_tree(root, "private-SID", writable=True)
    assert events == [
        ("open", "private-SID", True),
        ("seal", root, {}),
        ("seal", "sub", {"directory": True, "dir_fd": 101}),
        ("seal", "a.py", {"directory": False, "dir_fd": 101}),
        ("seal", "b.py", {"directory": False, "dir_fd": 202}),
        ("free",),
    ]


@pytest.mark.parametrize("mode,links", [(stat.S_IFLNK, 1), (stat.S_IFREG, 2), (stat.S_IFIFO, 1)])
def test_tree_rejects_links_and_releases_descriptor(sealing, monkeypatch, mode, links):
    security, events, root = sealing
    monkeypatch.setattr(
        security_module.windows_files,
        "stat_at",
        lambda *a, **kw: SimpleNamespace(st_mode=mode, st_nlink=links),
    )
    with pytest.raises(ValueError, match="link or special"):
        security.seal_tree(root, "private-SID")
    assert events[-1] == ("free",)
    assert [event[1] for event in events if event[0] == "seal"] == [root]


def test_tree_cancellation_releases_descriptor_before_launch(sealing):
    security, events, root = sealing
    context = CancellationContext()
    context.cancel()
    with cancellation_scope(context), pytest.raises(RunCancelled):
        security.seal_tree(root, "private-SID")
    assert events == [("open", "private-SID", False), ("free",)]


@pytest.mark.parametrize("failure", [False, True])
def test_acl_update_keeps_protection_flags_and_closes_resources(tmp_path, monkeypatch, failure):
    security = object.__new__(PrivateWindowsSecurity)
    events = []
    opened = []

    def convert(sddl, version, output, size):
        output._obj.value = 42
        return 1

    def get_dacl(descriptor, present, output, defaulted):
        present._obj.value = 1
        output._obj.value = 99
        return 1

    def set_security(handle, kind, flags, owner, group, dacl, sacl):
        events.append((kind, flags, owner, group, dacl.value, sacl))
        return 5 if failure else 0

    security.advapi = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=convert,
        GetSecurityDescriptorDacl=get_dacl,
        SetSecurityInfo=set_security,
    )
    security.api = SimpleNamespace(
        check=lambda result: result,
        kernel=SimpleNamespace(
            LocalFree=lambda descriptor: events.append(("free", descriptor.value))
        ),
    )
    security.user_sid = "owner"

    def open_file(name, parent, access, *, directory):
        assert (name, parent, access, directory) == ("file", 101, 0x60000, False)
        fd = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600)
        opened.append(fd)
        return fd

    @contextmanager
    def parent(path, fd):
        assert (path, fd) == ("file", 101)
        yield fd, path

    monkeypatch.setattr(security_module.windows_files, "_parent", parent)
    monkeypatch.setattr(
        security_module.windows_files,
        "_api",
        lambda: SimpleNamespace(open=open_file, handle=lambda fd: fd),
    )
    monkeypatch.setattr(
        security_module.C, "WinError", lambda code: OSError(code, "ACL failed"), raising=False
    )

    def update():
        with security._dacl("private-SID", writable=False) as dacl:
            security._set_acl("file", dacl, directory=False, dir_fd=101)

    if failure:
        with pytest.raises(OSError, match="ACL failed"):
            update()
    else:
        update()
    assert events == [(1, 0x80000004, None, None, 99, None), ("free", 42)]
    with pytest.raises(OSError):
        os.fstat(opened[0])
