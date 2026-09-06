import os

CACHE = {}


def hash_value(v: str) -> str:
    """Hashes the value."""
    return str(len(v))


def normalize(s: str) -> str:
    return s.strip().lower()
