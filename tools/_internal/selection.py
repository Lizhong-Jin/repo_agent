"""Keep a deterministic smallest K without retaining the entire input."""

from dataclasses import dataclass
from heapq import heappush, heapreplace
from typing import Any


@dataclass
class _LargestFirst:
    key: Any
    item: Any

    def __lt__(self, other):
        return self.key > other.key


class SmallestItems:
    """O(K) retained items, O(N log K) selection; ties preserve input order."""

    def __init__(self, limit, *, key):
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        self.limit = limit
        self.key = key
        self.total = 0
        self._heap = []

    def add(self, item):
        candidate = _LargestFirst((self.key(item), self.total), item)
        self.total += 1
        if len(self._heap) < self.limit:
            heappush(self._heap, candidate)
        elif candidate.key < self._heap[0].key:
            heapreplace(self._heap, candidate)

    def sorted_items(self):
        return [entry.item for entry in sorted(self._heap, key=lambda entry: entry.key)]
