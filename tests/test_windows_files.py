"""Windows policy tests everywhere; kernel tests only on real Windows."""

import ctypes
import os
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest

from host_support import filesystem as fs
from host_support import windows_files as win
from host_support.storage import atomic_write
from tools._internal.file_access import FileAccess

windows = pytest.mark.skipif(os.name != "nt", reason="Requires Windows kernel APIs")


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "C:relative",
        "file:stream",
        "file.",
        "file ",
        "NUL",
        "con.txt",
        "COM1.log",
        "LPT¹.txt",
        "CONOUT$",
        "foo\0bar",
        "a?",
        "a*",
    ],
)
def test_windows_component_aliases_are_rejected(name):
    with pytest.raises(ValueError):
        win.validate_component(name)


@pytest.mark.parametrize(
    "names",
    [
        ["a.txt", "A.txt"],
        ["Dir/a", "dir/b"],
        ["../outside"],
        ["/absolute"],
        ["file:stream"],
        ["dir//file"],
        [".git./config"],
        ["dir/NUL.txt"],
        ["C:/file"],
    ],
)
def test_windows_snapshot_paths_have_one_interpretation(names):
    with pytest.raises(ValueError):
        win.validate_snapshot_names(names)


def test_unicode_and_spaces_and_ctypes_abi():
    win.validate_snapshot_names(["目录 with spaces/😀.txt", "目录 with spaces/other"])
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(win._ObjectAttributes) == 48
        assert ctypes.sizeof(win._IOStatus) == 16
        assert ctypes.sizeof(win._BasicInfo) == 40
        assert win._RenameInfo.FileName.offset == 20
        assert ctypes.sizeof(win._Overlapped) == 32


def test_windows_git_lookup_does_not_execute_from_workspace(tmp_path, monkeypatch):
    from host_support.paths import find_windows_executable

    project, trusted = tmp_path / "project", tmp_path / "installed tools"
    project.mkdir()
    trusted.mkdir()
    (project / "git.exe").write_bytes(b"project-controlled")
    (trusted / "git.exe").write_bytes(b"installed")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(project), str(trusted))))
    assert find_windows_executable("git", exclude=(project,)) == str(trusted / "git.exe")
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(project))))
    assert find_windows_executable("git", exclude=(project,)) is None


@pytest.fixture
def junction(tmp_path):
    target, link = tmp_path / "outside", tmp_path / "junction"
    target.mkdir()
    (target / "sentinel").write_bytes(b"outside")
    # Junction creation does not require Developer Mode or symlink privilege.
    subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )
    try:
        yield link, target
    finally:
        if link.exists():
            link.rmdir()  # Remove only the junction, never recurse through it.


@windows
def test_junctions_are_not_followed_in_absolute_or_relative_opens(tmp_path, junction):
    link, target = junction
    with pytest.raises(OSError):
        fs.open_file(link / "sentinel")
    with pytest.raises(OSError):
        fs.open_directory(link)
    parent = fs.open_directory(tmp_path)
    try:
        assert stat.S_ISLNK(fs.stat_at(link.name, dir_fd=parent).st_mode)
        with pytest.raises(OSError):
            fs.open_directory(link.name, dir_fd=parent)
    finally:
        os.close(parent)
    assert (target / "sentinel").read_bytes() == b"outside"


@windows
def test_snapshot_skips_junctions_and_strict_export_rejects_them(tmp_path, junction):
    from sandbox.policy import SandboxPolicy
    from sandbox.session import files

    link, _ = junction
    result = files(tmp_path, SandboxPolicy())
    assert not any(name.startswith(link.name + "/") for name in result)
    with pytest.raises(ValueError, match="非普通文件"):
        files(tmp_path, SandboxPolicy(), strict=True)


@windows
def test_open_truncate_never_modifies_hardlink_target(tmp_path):
    target, link = tmp_path / "target", tmp_path / "link"
    target.write_bytes(b"keep")
    os.link(target, link)
    with pytest.raises(PermissionError):
        fs.open_file(link, os.O_WRONLY | os.O_TRUNC)
    assert target.read_bytes() == b"keep"


@windows
def test_atomic_replace_of_readonly_file_and_failure_cleanup(tmp_path):
    target = tmp_path / "readonly"
    atomic_write(target, b"before", mode=0o400)
    try:
        atomic_write(target, b"after", mode=0o400)
        assert target.read_bytes() == b"after"

        def fail():
            raise OSError("stop")

        with pytest.raises(OSError, match="stop"):
            atomic_write(target, b"lost", mode=0o400, before_replace=fail)
        assert list(tmp_path.iterdir()) == [target]
        assert target.read_bytes() == b"after"
    finally:
        target.chmod(0o600)


@windows
def test_stage_parent_swap_does_not_write_through_junction(tmp_path):
    parent, moved, outside = tmp_path / "parent", tmp_path / "moved", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    (outside / "file").write_bytes(b"outside")
    with FileAccess(tmp_path).activate() as access:
        staged = access.stage(parent / "file", b"new", 0o644)
        try:
            try:
                parent.rename(moved)
            except PermissionError:
                # Some Windows filesystems deny moving a pinned directory.
                # That is also a safe outcome: the old parent remains pinned.
                staged.replace(parent / "file")
                assert (parent / "file").read_bytes() == b"new"
            else:
                subprocess.run(
                    [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(parent), str(outside)],
                    check=True,
                    capture_output=True,
                )
                try:
                    with pytest.raises(OSError):
                        staged.replace(parent / "file")
                finally:
                    parent.rmdir()
        finally:
            staged.unlink(missing_ok=True)
    assert (outside / "file").read_bytes() == b"outside"
    assert not list(tmp_path.rglob(".repo-agent-write-*"))


@windows
def test_all_local_file_tools_use_windows_service(tmp_path):
    from tools.factory import create_file_tools

    tools = {tool.definition.name: tool for tool in create_file_tools(tmp_path)}

    def call(name, **arguments):
        result = tools[name].execute(arguments)
        assert result.success, result
        return result

    call("make_directory", path="src/nested", parents=True)
    call("write_file", path="src/a.bat", content="hello\n")
    call("read_file", reads=[{"path": "src/a.bat"}])
    call("edit_file", path="src/a.bat", edits=[{"old_text": "hello", "new_text": "world"}])
    call(
        "apply_patch",
        patch=(
            "*** Begin Patch\n*** Update File: src/a.bat\n@@\n-world\n+changed\n*** End Patch\n"
        ),
    )
    call("list_files", path="src")
    call("find_files", pattern="**/*.bat")
    call("search_files", query="changed")
    call("get_path_info", path="src/a.bat")
    call("move_file", source="src/a.bat", destination="dst/a.bat", create_parents=True)
    assert (tmp_path / "dst/a.bat").read_text() == "changed\n"
    call("delete_file", path="dst/a.bat")
    denied = tools["write_file"].execute({"path": "normal.txt:stream", "content": "secret"})
    assert not denied.success


@windows
def test_skills_scan_uses_directory_handles(tmp_path):
    from agent.skills.registry import SkillRegistry

    folder = tmp_path / "skills/example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example skill\n---\nUse this skill.\n", encoding="utf-8"
    )
    assert SkillRegistry(tmp_path).get("example").name == "example"


@windows
def test_writeback_junction_swap_fails_without_changing_outside(tmp_path, monkeypatch):
    from sandbox.session import SandboxSession
    from sandbox.writeback import Backup

    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "file").write_bytes(b"outside")
    session = SandboxSession(project, backend=SimpleNamespace(healthy=True))
    try:
        (session.workspace / "nested").mkdir()
        (session.workspace / "nested/file").write_bytes(b"new")
        original = Backup.__init__

        def replace_parent(self, *args, **kwargs):
            original(self, *args, **kwargs)
            subprocess.run(
                [
                    os.environ["COMSPEC"],
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(project / "nested"),
                    str(outside),
                ],
                check=True,
                capture_output=True,
            )

        monkeypatch.setattr(Backup, "__init__", replace_parent)
        with pytest.raises(OSError):
            session.apply()
        assert (outside / "file").read_bytes() == b"outside"
    finally:
        if (project / "nested").exists():
            (project / "nested").rmdir()
        shutil.rmtree(session.directory)


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("RUN_WINDOWS_DOCKER_TESTS") != "1",
    reason="Requires Windows Docker Desktop Linux containers and local image",
)
def test_real_windows_docker_roundtrip(tmp_path):
    from sandbox.session import SandboxSession

    project = tmp_path / "中文 workspace"
    project.mkdir()
    (project / "hello.txt").write_bytes(b"before\r\n")
    session = SandboxSession(project)
    try:
        result = session.backend.execute(
            session.workspace,
            "write_file",
            {
                "path": "hello.txt",
                "content": "after\n",
                "overwrite": True,
            },
        )
        assert result.success, result
        assert session.apply() == ["hello.txt"]
        backup_id = session.last_backup.name
        assert (project / "hello.txt").read_bytes() == b"after\n"
        assert session.restore(backup_id) == ["hello.txt"]
        assert (project / "hello.txt").read_bytes() == b"before\r\n"
    finally:
        shutil.rmtree(session.directory)
