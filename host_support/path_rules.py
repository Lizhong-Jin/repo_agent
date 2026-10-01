"""Pure name matching mechanism; callers supply all application policy values."""

from dataclasses import dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class NameRules:
    names: frozenset[str]
    prefixes: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()

    def matches_leaf(self, name: str) -> bool:
        # Preserve Unicode lower(), not casefold() or ASCII-only conversion.
        name = name.lower()
        return name in self.names or name.startswith(self.prefixes) or name.endswith(self.suffixes)

    def matches_path(self, path: str | PurePath) -> bool:
        return any(self.matches_leaf(part) for part in PurePath(path).parts)
