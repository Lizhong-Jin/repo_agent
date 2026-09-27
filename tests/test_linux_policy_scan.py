"""Protection equivalence and fresh filesystem observations after scan optimization."""

import errno
import os
from collections import Counter
from pathlib import Path

import pytest

from sandbox import linux_policy as policy_module
from sandbox.linux_policy import PolicyScan, outermost
from scripts.benchmark_linux_policy import (
    _outermost as reference_outermost,
)
from scripts.benchmark_linux_policy import policy_backend, reference_mount_policy
from tools._internal.file_policy import (
    PROTECTED_NAMES,
    PROTECTED_SUFFIXES,
    is_protected_leaf,
    is_protected_name,
)


@pytest.fixture
def tree(tmp_path):
    # Resolve macOS /var and /tmp aliases before asserting canonical scan counts.
    base = tmp_path.resolve()
    workspace, system = base / "project", base / "usr"
    workspace.mkdir()
    library = system / "lib"
    library.mkdir(parents=True)
    alias = base / "lib"
    alias.symlink_to(library, target_is_directory=True)
    return policy_backend(workspace, (system, alias)), system, library, alias


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def assert_reference(backend, *, git_read=False):
    expected = reference_mount_policy(backend, backend.read_paths, git_read=git_read)
    actual = backend._mount_policy(backend.read_paths, git_read=git_read)
    assert tuple(map(set, actual)) == tuple(map(set, expected))
    return actual


@pytest.mark.parametrize("name,expected", [
    *((name, True) for name in PROTECTED_NAMES),
    *(("test" + suffix, True) for suffix in PROTECTED_SUFFIXES),
    (".env", True), (".env.local", True), ("normal.py", False),
    ("env", False), ("token.pem.txt", False), (".environment", False),
])
def test_leaf_matcher_keeps_case_and_suffix_semantics(name, expected):
    for variant in (name, name.upper(), name.title()):
        assert is_protected_leaf(variant) == expected
        assert is_protected_name(Path("ordinary") / variant / "file") == expected


@pytest.mark.parametrize("git_read", [False, True])
@pytest.mark.parametrize("fixed_view", ["canonical", "alias", "both"])
def test_alias_mapping_matches_old_policy_with_view_specific_paths(tree, git_read, fixed_view):
    backend, system, library, alias = tree
    for name in ("ok.py", ".ENV", "secret.PEM", ".git/config", ".git/logs/data",
                 ".git/.env.local", "nested/private", "nested/private-tree/file"):
        touch(library / name)
    touch(backend.workspace / ".env")
    touch(backend.workspace / "normal.py")
    prefixes = {"canonical": (library,), "alias": (alias,), "both": (library, alias)}
    backend.protected_paths = tuple(
        prefix / leaf for prefix in prefixes[fixed_view]
        for leaf in ("nested/private", "nested/private-tree")
    )
    assert_reference(backend, git_read=git_read)
    assert backend.last_policy_metrics["directories_reused"] > 0


@pytest.mark.parametrize("git_read", [False, True])
def test_root_protection_and_pruned_subtree_do_not_hide_an_alias_from_scan(tree, git_read):
    backend, system, library, alias = tree
    hidden = library / "logs"
    touch(hidden / "nested/.env")
    view = alias.parent / "exposed"
    view.symlink_to(hidden, target_is_directory=True)
    backend.read_paths = (system, view)
    masks, _ = assert_reference(backend, git_read=git_read)
    assert hidden in masks and view / "nested/.env" in masks


def test_each_real_readonly_directory_is_enumerated_once(tree, monkeypatch):
    backend, _, library, alias = tree
    for number in range(1000):
        touch(library / "package" / f"module-{number}.py")
    touch(library / "package/.env")
    calls = Counter()
    original = policy_module.open_directory

    def counted(path):
        calls[Path(path).resolve()] += 1
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(policy_module, "open_directory", counted)
        masks, _ = backend._mount_policy(backend.read_paths, git_read=False)
    assert set(masks) == {library / "package/.env", alias / "package/.env"}
    assert set(calls.values()) == {1}
    assert backend.last_policy_metrics["directories_reused"] == 2
    assert backend.last_policy_metrics["entries_classified"] == 1003


def test_new_nested_secrets_are_found_on_each_call_in_all_views(tree):
    backend, _, library, alias = tree
    touch(library / "deep/package/normal.py")
    backend._mount_policy(backend.read_paths, git_read=False)
    plan = backend._policy_plan()
    touch(library / "deep/package/.env.new")
    touch(backend.workspace / "src/.env.new")
    masks, _ = assert_reference(backend)
    assert set(masks) == {library / "deep/package/.env.new", alias / "deep/package/.env.new",
                          backend.workspace / "src/.env.new"}
    assert backend._policy_plan() is plan  # Only pure configuration is reused.
    assert not hasattr(plan, "cache")


def test_retargeted_alias_is_resolved_again_on_next_call(tree):
    backend, _, library, alias = tree
    touch(library / ".env.old")
    assert_reference(backend)
    other = touch(alias.parent / "other/nested/.env.new").parents[1]
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    masks, _ = assert_reference(backend)
    assert alias / "nested/.env.new" in masks
    assert alias / ".env.old" not in masks


def test_static_plan_refreshes_for_changed_configuration_and_checks_new_guards(tree):
    backend, _, library, _ = tree
    initial = backend._policy_plan()
    secret = touch(library / "custom-secret")
    backend.protected_paths = (secret,)
    assert secret in assert_reference(backend)[0]
    assert backend._policy_plan() is not initial
    previous = backend._policy_plan()
    backend.read_paths = (*backend.read_paths, library / "extra")
    assert backend._policy_plan() is not previous


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
def test_unreadable_subtree_masks_every_alias_and_permissions_are_fresh(tree, monkeypatch,
                                                                      error_number):
    backend, _, library, alias = tree
    restricted = touch(library / "modules/lost+found/.env").parent
    original = os.scandir
    original_open = policy_module.open_directory
    denied = True

    def opened(path):
        if denied and Path(path).resolve() == restricted:
            raise PermissionError(error_number, "denied", str(path))
        return original_open(path)

    def scandir(path):
        if isinstance(path, int):
            return original(path)
        if denied and Path(path).resolve() == restricted:
            raise PermissionError(error_number, "denied", str(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(policy_module, "open_directory", opened)
    masks, _ = assert_reference(backend)
    assert set(masks) == {restricted, alias / "modules/lost+found"}
    denied = False
    masks, _ = assert_reference(backend)
    assert set(masks) == {restricted / ".env", alias / "modules/lost+found/.env"}


def test_symlinks_are_never_recursively_followed(tree):
    backend, _, library, _ = tree
    outside = touch(library.parent.parent / "outside/.env").parent
    (library / "ordinary-link").symlink_to(outside, target_is_directory=True)
    (library / "dangling").symlink_to("missing")
    assert assert_reference(backend) == ([], [])


def test_protected_system_symlink_masks_containing_directory_in_each_view(tree):
    backend, _, library, alias = tree
    certs = library / "certs"
    certs.mkdir()
    (certs / "cert.pem").symlink_to("/nonexistent-target")
    masks, _ = assert_reference(backend)
    assert set(masks) == {certs, alias / "certs"}


def test_sparse_cache_does_not_retain_ordinary_files(tree):
    backend, _, library, _ = tree
    for number in range(100):
        touch(library / f"file-{number}.py")
    touch(library / ".env")
    scan = PolicyScan(backend._policy_plan(), git_read=False)
    facts = scan._entries(str(library), str(library), True)
    assert [entry[0] for entry in facts] == [".env"]
    scan.run(backend.read_paths)
    assert scan.cache == {}


def test_alias_changed_during_scan_fails_closed(tree, monkeypatch):
    backend, _, library, alias = tree
    touch(library / ".env")
    other = alias.parent / "other"
    other.mkdir()
    original = PolicyScan._entries

    def entries(self, directory, canonical, reuse):
        facts = original(self, directory, canonical, reuse)
        if directory == str(alias):
            alias.unlink()
            alias.symlink_to(other, target_is_directory=True)
        return facts

    monkeypatch.setattr(PolicyScan, "_entries", entries)
    with pytest.raises(ValueError, match="挂载目录发生变化"):
        backend._mount_policy(backend.read_paths, git_read=False)
    assert backend.last_policy_metrics["complete"] is False


def test_directory_changed_to_symlink_before_descent_fails_closed(tree, monkeypatch):
    backend, _, library, _ = tree
    child = library / "child"
    child.mkdir()
    outside = touch(library.parent.parent / "outside/.env").parent
    original = PolicyScan._entries

    def entries(self, directory, canonical, reuse):
        facts = original(self, directory, canonical, reuse)
        if canonical == str(library):
            child.rmdir()
            child.symlink_to(outside, target_is_directory=True)
        return facts

    monkeypatch.setattr(PolicyScan, "_entries", entries)
    with pytest.raises(ValueError, match="变为符号链接"):
        backend._mount_policy(backend.read_paths, git_read=False)


def test_mount_pruning_matches_reference_with_many_siblings_and_similar_prefixes():
    paths = [Path(f"/root/tree-{i}/nested/file") for i in range(400)]
    paths += [Path(f"/root/tree-{i}") for i in range(0, 400, 3)]
    paths += [Path("/root/tree-1/nested"), Path("/root/tree-10/nested"), Path("/alias")]
    assert outermost(paths) == reference_outermost(paths)


@pytest.mark.skipif(os.geteuid() == 0, reason="Requires ordinary-user filesystem permissions")
@pytest.mark.parametrize("mode", [0o000, 0o111])
def test_real_unreadable_directory_is_masked_without_os_sandbox(tree, mode):
    backend, _, library, alias = tree
    restricted = touch(library / "lost+found/.env").parent
    restricted.chmod(mode)
    try:
        if mode == 0o111:
            assert (restricted / ".env").read_text() == ""  # Known paths are still readable.
        masks, _ = assert_reference(backend)
        assert set(masks) == {restricted, alias / "lost+found"}
    finally:
        restricted.chmod(0o700)


def test_alias_replay_still_checks_directory_type(tree, monkeypatch):
    backend, system, library, alias = tree
    child = library / 'child'
    child.mkdir()
    outside = library.parent.parent / 'outside'
    outside.mkdir()
    original = PolicyScan._entries

    def entries(self, directory, canonical, reuse):
        # /lib is scanned before /usr. Change only after its child facts were cached.
        if directory == str(system):
            child.rmdir()
            child.symlink_to(outside, target_is_directory=True)
        return original(self, directory, canonical, reuse)

    monkeypatch.setattr(PolicyScan, '_entries', entries)
    with pytest.raises(ValueError, match='变为符号链接'):
        backend._mount_policy(backend.read_paths, git_read=False)


def test_metadata_timing_and_alias_replay_counts_are_separate(tree):
    backend, _, library, _ = tree
    touch(library / 'package/normal.py')
    backend._mount_policy(backend.read_paths, git_read=False)
    metrics = backend.last_policy_metrics
    assert metrics['directory_opens'] == metrics['directories_scanned']
    assert metrics['metadata_checks'] == metrics['directories_reused'] == 2
    assert metrics['metadata_ms'] > 0
    assert sum(group.get('metadata_checks', 0) for group in metrics['by_root_mount']) == 2
    assert metrics['scan_ms'] >= sum(metrics[key] for key in (
        'metadata_ms', 'rules_ms', 'enumeration_ms', 'workspace_validation_ms'))
