"""Trusted Python packages; independent of installation and task directories."""

from pathlib import Path


def trusted_code_root():
    return Path(__file__).resolve().parents[1]
