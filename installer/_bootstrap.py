"""Make sibling stdlib services available to direct installer/recovery scripts."""

import sys
from pathlib import Path


def enable_host_support():
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
