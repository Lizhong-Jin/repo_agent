"""Materialize authorized inputs without exposing host paths to child processes."""

import os
from dataclasses import replace
from pathlib import Path

from .windows_workspace import copy_private_tree


def stage_permission_inputs(backend, destination):
    manager = getattr(backend, "permissions", None)
    paths, mapping = {}, []
    if manager is None:
        return paths, mapping
    with manager.lock:
        manager.protect_execution_path(destination.parent)
        for item in manager.profile.read_only_roots:
            grant = manager.root(item.id)
            policy = manager.path_policy()
            # Windows profiles can be outside the backend control directory. A
            # broad input grant must never copy this call's destination into itself.
            policy = replace(
                policy,
                protected_paths=(
                    *policy.protected_paths,
                    *getattr(backend, "protected_paths", ()),
                ),
            )
            if policy.protects_path(grant.path, grant.path):
                raise ValueError("Authorized input overlaps private execution state")
            target = destination / grant.id
            copy_private_tree(
                grant.path,
                target,
                allowed=lambda relative, policy=policy, grant=grant: (
                    not policy.protects_path(grant.path / relative, grant.path / relative)
                ),
                limit=256 * 1024**2,
                source_identity=grant.identity,
                preserve_executable=True,
                max_entries=100000,
                windows_names=os.name == "nt",
            )
            paths[grant.id] = str(target)
            mapping.append((grant.path, target))
    return paths, mapping


def map_input_arguments(command, mapping):
    """Only whole absolute argv paths; never rewrite shell/Python source text."""
    if not command or not mapping:
        return command

    def mapped(value):
        path = Path(value)
        if path.is_absolute() and ".." not in path.parts:
            # Most specific grant wins when authorized roots overlap.
            for source, target in sorted(
                mapping, key=lambda pair: len(pair[0].parts), reverse=True
            ):
                if path.is_relative_to(source):
                    return str(target / path.relative_to(source))
        return value

    return tuple(mapped(value) for value in command)
