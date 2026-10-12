"""Download an explicit older stable release for upgrade acceptance, never execute it here."""

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]


def select(releases, current, requested="auto"):
    candidates = []
    for release in releases:
        tag = release["tagName"]
        if release["isDraft"] or release["isPrerelease"] or not tag.startswith("v"):
            continue
        try:
            version = Version(tag[1:])
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease or version >= Version(current):
            continue
        if requested != "auto" and tag != requested:
            continue
        candidates.append((version, tag))
    if not candidates:
        raise ValueError("No older stable release available; choose a valid previous_tag")
    return max(candidates)[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    repository = os.environ["GITHUB_REPOSITORY"]
    releases = json.loads(
        subprocess.check_output(
            [
                "gh",
                "release",
                "list",
                "--repo",
                repository,
                "--limit",
                "100",
                "--json",
                "tagName,isDraft,isPrerelease",
            ],
            text=True,
        )
    )
    tag = select(releases, current, os.environ.get("PREVIOUS_TAG") or "auto")
    suffix = ".zip" if args.target.startswith("windows") else ".tar.gz"
    name = f"repo-agent-{tag[1:]}-{args.target}{suffix}"
    args.output.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--dir",
            str(args.output),
            "--pattern",
            name,
            "--pattern",
            name + ".sha256",
        ],
        check=True,
    )
    from release_acceptance import sha256

    archive = (args.output / name).resolve()
    if archive.with_name(name + ".sha256").read_text().strip() != f"{sha256(archive)}  {name}":
        raise ValueError("Previous release checksum mismatch")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
        stream.write(f"REPO_AGENT_PREVIOUS_ARCHIVE={archive}\n")
    print(f"Upgrade baseline: {tag}, {name}, {sha256(archive)}")


if __name__ == "__main__":
    main()
