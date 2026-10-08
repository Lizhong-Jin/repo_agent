"""Inspect Git filter selection without running a content conversion command."""

import re


class GitFilterCheckError(ValueError):
    """The preflight response was incomplete or invalid."""


class ExternalGitFilter(ValueError):
    """A repository path selects an external content filter."""


def nul_fields(text):
    if not text:
        return []
    if not text.endswith("\0") or "\ufffd" in text:
        raise GitFilterCheckError("Incomplete Git metadata")
    return text[:-1].split("\0")


def check_external_filters(command, *, checkout=False):
    """command(args, allowed=...) returns complete, bounded stdout or raises.

    Include index attributes as well as worktree attributes: an unstaged edit
    must not hide a driver used when checking out the index. Git itself resolves
    nested attributes, macros, info/attributes and global attributes. Like other
    Git preflights, this is not atomic against concurrent external config edits.
    """
    kinds = "clean|smudge|process" if checkout else "clean|process"
    config = command(
        ["config", "--includes", "--null", "--get-regexp", rf"^filter\..*\.({kinds})$"],
        allowed=(0, 1),
    )
    effective = {}
    for entry in nul_fields(config):
        key, separator, value = entry.partition("\n")
        match = re.fullmatch(rf"filter\.(.+)\.({kinds})", key)
        if not separator or not match:
            raise GitFilterCheckError("Invalid Git filter configuration")
        effective[(match[1], match[2])] = value
    drivers = {name for (name, _), value in effective.items() if value}
    if not drivers:
        return
    paths = sorted(
        set(nul_fields(command(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])))
    )
    # Bound argv size for Windows as well as POSIX, including non-ASCII names.
    batches, batch, size = [], [], 0
    for path in paths:
        if not path:
            raise GitFilterCheckError("Invalid Git path")
        width = len(path.encode("utf-8", "surrogatepass")) + 3
        if width > 16000:
            raise GitFilterCheckError("Git path exceeds preflight limit")
        if size + width > 16000:
            batches.append(batch)
            batch, size = [], 0
        batch.append(path)
        size += width
    if batch:
        batches.append(batch)
    for batch in batches:
        for options in ([], ["--cached"]):
            fields = nul_fields(command(["check-attr", *options, "-z", "filter", "--", *batch]))
            if len(fields) != 3 * len(batch):
                raise GitFilterCheckError("Incomplete Git attribute response")
            for index, path in enumerate(batch):
                name, attribute, value = fields[index * 3 : index * 3 + 3]
                if name != path or attribute != "filter":
                    raise GitFilterCheckError("Invalid Git attribute response")
                if value in drivers:
                    raise ExternalGitFilter("Repository paths select external Git filters")
