"""Setuptools adapter for the shared, standard-library distribution manifest."""

import shutil
import sys
import tarfile
from pathlib import Path

from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

# Setuptools can load this hook by file path without putting the source root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_manifest import (  # noqa: E402
    CONTEXT_ARCHIVE,
    PACKAGES,
    RESOURCE_FILES,
    check_configuration,
    source_files,
)


class BuildPy(build_py):
    def run(self):
        root = Path(__file__).resolve().parent
        check_configuration(root)
        sources = source_files(root)  # Validate everything before producing partial output.
        # A previous non-editable build must not leak removed modules/resources into a wheel.
        for package in PACKAGES:
            directory = Path(self.build_lib) / package
            if directory.is_dir():
                shutil.rmtree(directory)
        super().run()
        for source, destination in RESOURCE_FILES:
            target = Path(self.build_lib) / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / source, target)
        target = Path(self.build_lib) / CONTEXT_ARCHIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(target, "w:gz") as bundle:
            for path in sources:
                bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)


class Sdist(sdist):
    def run(self):
        root = Path(__file__).resolve().parent
        check_configuration(root)
        source_files(root)
        super().run()

    def make_release_tree(self, base_dir, files):
        root = Path(__file__).resolve().parent
        allowed = {path.relative_to(root).as_posix() for path in source_files(root)}
        missing = allowed - set(files)
        if missing:
            raise ValueError(f"Source distribution is missing declared inputs: {sorted(missing)}")
        # Retain setuptools-owned metadata, but do not let its recursive manifest or
        # an old egg-info cache add undeclared files from the working checkout.
        metadata = {
            name
            for name in files
            if len(Path(name).parts) == 2 and Path(name).parts[0].endswith(".egg-info")
        }
        super().make_release_tree(base_dir, sorted(allowed | metadata))
